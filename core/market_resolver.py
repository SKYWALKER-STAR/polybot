"""
BTC 5-minute market resolver.

Polymarket 上的 BTC 5-分钟涨跌市场每隔 5 分钟开一个新市场，slug 格式为：
    btc-updown-5m-{unix_timestamp}

其中时间戳是该市场的 **结束时间**（同时也是下一个市场的开始时间）。

工作流程：
    1. 用初始时间戳拼接 slug，查询 Gamma API 获取完整市场信息。
    2. 比较当前 UTC 时间与市场的 startDate / endDate，判断是否在有效期内。
    3. 市场到期后，从 endDate 解析出下一个时间戳，拼接新 slug，重复步骤 1。
    4. 对外暴露 get_active_market()，始终返回当前有效的市场信息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
SLUG_PREFIX = "btc-updown-5m"
HTTP_TIMEOUT = 10.0

# 所有时间显示与比较均使用美东时间（自动处理 EST/EDT 夏令时切换）
ET_TZ = ZoneInfo("America/New_York")


@dataclass
class MarketInfo:
    """当前活跃的 BTC 5-min 市场的完整标识信息。"""

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    start_time: datetime   # ET
    end_time: datetime     # ET
    up_price: Optional[float] = None    # outcomePrices[0] from Gamma API
    down_price: Optional[float] = None  # outcomePrices[1] from Gamma API

    @property
    def is_active(self) -> bool:
        """当前 ET 时间是否在 [start_time, end_time) 范围内。"""
        now = datetime.now(ET_TZ)
        return self.start_time <= now < self.end_time

    @property
    def is_expired(self) -> bool:
        """市场是否已经结算。"""
        return datetime.now(ET_TZ) >= self.end_time

    @property
    def seconds_to_expiry(self) -> float:
        """距结算还剩多少秒（负数表示已过期）。"""
        return (self.end_time - datetime.now(ET_TZ)).total_seconds()

    @property
    def next_slug_timestamp(self) -> int:
        """
        下一个市场的 slug 时间戳。
        slug 中的时间戳是该市场的**开始时间**，两个市场首尾相连，
        因此下一个市场的开始时间 = 当前市场的 end_time。
        """
        return int(self.end_time.timestamp())


class MarketResolver:
    """
    根据初始时间戳动态解析并跟踪当前活跃的 BTC 5-min 市场。

    用法::

        resolver = MarketResolver(initial_timestamp=1780073700)
        info = await resolver.get_active_market()
        print(info.condition_id, info.up_token_id, info.down_token_id)
    """

    def __init__(self, initial_timestamp: int) -> None:
        self._current_timestamp: int = initial_timestamp
        self._cached: Optional[MarketInfo] = None

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    async def get_active_market(self) -> MarketInfo:
        """
        返回当前有效的市场信息。
        - 若缓存有效（市场未到期），直接返回缓存。
        - 若市场已到期，自动推进到下一个市场并重新查询。
        """
        # 缓存命中且市场仍有效
        if self._cached is not None and not self._cached.is_expired:
            return self._cached

        # 需要推进：若有过期缓存，从其 end_time 推导下一个时间戳
        if self._cached is not None and self._cached.is_expired:
            next_ts = self._cached.next_slug_timestamp
            logger.info(
                "MarketResolver — 市场 %s 已结算，推进到下一个时间戳 %d",
                self._cached.slug,
                next_ts,
            )
            self._current_timestamp = next_ts

        # 从当前时间戳开始，依次向前查找直到找到活跃市场
        info = await self._resolve_active(self._current_timestamp)
        self._cached = info
        self._current_timestamp = int(info.end_time.timestamp())
        return info

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    async def _resolve_active(self, start_timestamp: int) -> MarketInfo:
        """
        从 start_timestamp 开始查找，若该市场已过期则继续向后推进，
        直到找到当前有效（is_active）或即将开始的市场为止。

        若 start_timestamp 比当前时间早超过 1 小时，说明配置的初始时间戳已严重过期，
        直接跳转到当前时间附近的 5 分钟边界开始搜索，避免无效遍历。
        """
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if now_ts - start_timestamp > 3600:
            # 对齐到最近的 5 分钟边界，再往前一个周期保证不遗漏
            aligned = (now_ts // 300) * 300 - 300
            logger.warning(
                "MarketResolver — 初始时间戳 %d 已过期超过 1 小时，"
                "自动跳转到当前时间附近: %d",
                start_timestamp,
                aligned,
            )
            start_timestamp = aligned

        ts = start_timestamp
        max_attempts = 20  # 防止死循环，最多向后查 20 个周期（100分钟）

        for attempt in range(max_attempts):
            slug = f"{SLUG_PREFIX}-{ts}"
            try:
                info = await self._fetch_by_slug(slug)
            except MarketNotFoundError:
                logger.warning("MarketResolver — slug %s 不存在，尝试下一个", slug)
                ts += 300
                continue
            except Exception as exc:
                logger.error("MarketResolver — 查询 slug %s 失败: %s", slug, exc)
                raise

            if info.is_active:
                logger.info(
                    "MarketResolver — 活跃市场: %s  结算时间(ET): %s  剩余: %.0fs",
                    slug,
                    info.end_time.isoformat(),
                    info.seconds_to_expiry,
                )
                return info

            if info.is_expired:
                logger.debug("MarketResolver — %s 已过期，尝试下一个时间戳 %d", slug, ts + 300)
                ts = info.next_slug_timestamp
                continue

            # 市场存在但尚未开始（未来市场），直接使用
            logger.info(
                "MarketResolver — 市场 %s 尚未开始，将在 %.0fs 后生效",
                slug,
                (info.start_time - datetime.now(ET_TZ)).total_seconds(),
            )
            return info

        raise RuntimeError(
            f"MarketResolver: 从时间戳 {start_timestamp} 开始尝试 {max_attempts} 次，"
            "仍未找到有效市场，请检查初始时间戳配置。"
        )

    async def _fetch_by_slug(self, slug: str) -> MarketInfo:
        """调用 Gamma API 查询 event，并解析为 MarketInfo。"""
        url = f"{GAMMA_API_BASE}/events"
        params = {"slug": slug}

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        # Gamma 返回列表，取第一个匹配的 event
        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            raise MarketNotFoundError(f"slug={slug!r} 在 Gamma API 中无结果")

        event = events[0]
        return self._parse_event(slug, event)

    @staticmethod
    def _parse_event(slug: str, event: dict) -> MarketInfo:
        """从 Gamma event dict 中解析出 MarketInfo。"""
        markets: list[dict] = event.get("markets") or []
        if not markets:
            raise ValueError(f"event {slug!r} 中 markets 列表为空")

        # 二元市场通常只有一个 market 条目，包含两个 token
        market = markets[0]

        condition_id: str = market.get("conditionId") or market.get("condition_id") or ""
        if not condition_id:
            raise ValueError(f"event {slug!r} 缺少 conditionId")

        # clobTokenIds 可能是 JSON 字符串或列表
        clob_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or []
        if isinstance(clob_ids, str):
            import json as _json
            try:
                clob_ids = _json.loads(clob_ids)
            except Exception:
                clob_ids = []

        if len(clob_ids) < 2:
            raise ValueError(f"event {slug!r} 的 clobTokenIds 不足两个: {clob_ids}")

        # 时间字段优先从 market 取，其次从 event 取
        start_str = (
            market.get("startDate")
            or market.get("start_date")
            or event.get("startDate")
            or event.get("start_date")
        )
        end_str = (
            market.get("endDate")
            or market.get("end_date")
            or event.get("endDate")
            or event.get("end_date")
        )

        if not start_str or not end_str:
            raise ValueError(f"event {slug!r} 缺少 startDate 或 endDate")

        start_time = _parse_dt(start_str)
        end_time = _parse_dt(end_str)

        # outcomePrices 与 outcomes 一一对应：["Up", "Down"] -> [up_price, down_price]
        import json as _json
        outcome_prices_raw = market.get("outcomePrices") or []
        if isinstance(outcome_prices_raw, str):
            try:
                outcome_prices_raw = _json.loads(outcome_prices_raw)
            except Exception:
                outcome_prices_raw = []
        up_price: Optional[float] = None
        down_price: Optional[float] = None
        if len(outcome_prices_raw) >= 2:
            try:
                up_price   = float(outcome_prices_raw[0])
                down_price = float(outcome_prices_raw[1])
            except (ValueError, TypeError):
                pass

        return MarketInfo(
            slug=slug,
            condition_id=condition_id,
            up_token_id=str(clob_ids[0]),
            down_token_id=str(clob_ids[1]),
            start_time=start_time,
            end_time=end_time,
            up_price=up_price,
            down_price=down_price,
        )


class MarketNotFoundError(Exception):
    """Gamma API 返回空结果时抛出。"""


def _parse_dt(value: str) -> datetime:
    """将 ISO-8601 字符串解析为带时区的 datetime（转换为 ET）。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET_TZ)
