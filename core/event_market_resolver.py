"""
Event Market Resolver + Data Service
======================================
Resolves a Polymarket multi-choice event by slug (e.g. an election market)
and provides per-outcome ``MarketData`` snapshots for downstream strategies.

每个多选事件（如大选）在 Polymarket 上由若干二元子市场组成——
每个候选结果（候选人）对应一个独立的 YES/NO 二元市场，具有独立的
conditionId 和 clobTokenIds。

本模块提供两个类：

``EventMarketResolver``
    通过事件 slug 向 Gamma API 查询所有子市场，并缓存结果（TTL 可配）。
    每个子市场解析为一个 ``OutcomeMarket``（包含标题、conditionId、token IDs）。

``EventMarketDataService``
    将 ``EventMarketResolver`` 与现有 ``WsMarketFeed`` 结合，
    为每个子市场的 YES/NO token 构建 ``MarketData`` 快照供策略使用。

选择监听的市场
--------------
通过环境变量 ``ELECTION_MARKET_SLUGS`` 指定事件 slug（逗号分隔，支持多个），例如：

    ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028
    # 或同时监听多个：
    ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028,republican-presidential-nominee-2028

slug 即 Polymarket 事件 URL 路径中最后一段，例如：
    https://polymarket.com/event/democratic-presidential-nominee-2028
                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Usage
-----
::

    resolver = EventMarketResolver("democratic-presidential-nominee-2028")
    info = await resolver.get_market_info()
    for market in info.markets:
        print(market.title, market.yes_token_id)
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.market_data import MarketData, OrderBookLevel
from core.ws_market_feed import WsMarketFeed

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
HTTP_TIMEOUT = 10.0


# ------------------------------------------------------------------ #
# 数据模型
# ------------------------------------------------------------------ #

@dataclass
class OutcomeMarket:
    """一个多选事件中的单个结果（如一位候选人）。"""

    title: str           # 结果名称，如 "Kamala Harris"
    condition_id: str    # 该子市场的 conditionId
    yes_token_id: str    # YES token（该候选人赢）
    no_token_id: str     # NO token（该候选人不赢）
    yes_price: Optional[float] = None   # Gamma API 给出的市场概率价格
    no_price: Optional[float] = None


@dataclass
class EventMarketInfo:
    """多选事件的完整快照（包含所有结果子市场）。"""

    event_slug: str
    event_title: str
    markets: list[OutcomeMarket]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ------------------------------------------------------------------ #
# Resolver
# ------------------------------------------------------------------ #

class EventMarketResolver:
    """
    通过事件 slug 查询 Gamma API 并缓存多选市场信息。

    缓存在 ``cache_ttl`` 秒后过期，届时重新向 Gamma API 拉取最新 outcomePrices。
    市场结构（tokenId / conditionId）在事件存续期内保持稳定，但 TTL 刷新可
    保证价格字段的准确性（用于日志展示）。
    """

    def __init__(self, event_slug: str, cache_ttl: float = 300.0) -> None:
        self._slug = event_slug
        self._cache_ttl = cache_ttl
        self._cached: Optional[EventMarketInfo] = None

    @property
    def event_slug(self) -> str:
        return self._slug

    async def get_market_info(self) -> EventMarketInfo:
        """返回缓存的市场信息（TTL 到期后自动重新拉取）。"""
        now = datetime.now(timezone.utc)
        if (
            self._cached is not None
            and (now - self._cached.fetched_at).total_seconds() < self._cache_ttl
        ):
            return self._cached

        info = await self._fetch()
        self._cached = info
        return info

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    async def _fetch(self) -> EventMarketInfo:
        url = f"{GAMMA_API_BASE}/events"
        params = {"slug": self._slug}

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            raise ValueError(
                f"EventMarketResolver: slug={self._slug!r} 在 Gamma API 中无结果"
            )

        event = events[0]
        event_title: str = event.get("title") or self._slug
        raw_markets: list[dict] = event.get("markets") or []

        if not raw_markets:
            raise ValueError(
                f"EventMarketResolver: event {self._slug!r} 下 markets 列表为空"
            )

        outcome_markets: list[OutcomeMarket] = []
        for m in raw_markets:
            om = self._parse_outcome_market(m)
            if om is not None:
                outcome_markets.append(om)

        if not outcome_markets:
            raise ValueError(
                f"EventMarketResolver: event {self._slug!r} 没有可解析的子市场"
            )

        logger.info(
            "[EventResolver:%s] 已加载 %d 个结果市场 | event: %s",
            self._slug, len(outcome_markets), event_title,
        )
        for om in outcome_markets:
            logger.debug(
                "[EventResolver:%s]   结果: %s | conditionId=%s | "
                "yes_token=%s | no_token=%s | yes_price=%.4f no_price=%.4f",
                self._slug, om.title, om.condition_id,
                om.yes_token_id[:8], om.no_token_id[:8],
                om.yes_price or 0.0, om.no_price or 0.0,
            )

        return EventMarketInfo(
            event_slug=self._slug,
            event_title=event_title,
            markets=outcome_markets,
        )

    @staticmethod
    def _parse_outcome_market(m: dict) -> Optional[OutcomeMarket]:
        """从 Gamma market dict 中解析为 OutcomeMarket，失败返回 None。"""
        condition_id: str = (
            m.get("conditionId") or m.get("condition_id") or ""
        )
        if not condition_id:
            label = m.get("question") or m.get("title") or "?"
            logger.warning(
                "EventMarketResolver: 市场 %r 缺少 conditionId，跳过", label
            )
            return None

        clob_ids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(clob_ids, str):
            try:
                clob_ids = _json.loads(clob_ids)
            except Exception:
                clob_ids = []

        if len(clob_ids) < 2:
            label = m.get("question") or m.get("title") or condition_id[:8]
            logger.warning(
                "EventMarketResolver: 市场 %r 的 clobTokenIds 不足 2 个（%s），跳过",
                label, clob_ids,
            )
            return None

        # outcomePrices: ["0.45", "0.55"] 或 JSON 字符串
        raw_prices = m.get("outcomePrices") or []
        if isinstance(raw_prices, str):
            try:
                raw_prices = _json.loads(raw_prices)
            except Exception:
                raw_prices = []

        yes_price: Optional[float] = None
        no_price: Optional[float] = None
        if len(raw_prices) >= 2:
            try:
                yes_price = float(raw_prices[0])
                no_price  = float(raw_prices[1])
            except (TypeError, ValueError):
                pass

        title: str = (
            m.get("question")
            or m.get("groupItemTitle")
            or m.get("title")
            or condition_id[:8]
        )

        return OutcomeMarket(
            title=title,
            condition_id=condition_id,
            yes_token_id=clob_ids[0],
            no_token_id=clob_ids[1],
            yes_price=yes_price,
            no_price=no_price,
        )


# ------------------------------------------------------------------ #
# Data Service
# ------------------------------------------------------------------ #

class EventMarketDataService:
    """
    为多选事件的所有结果子市场构建 MarketData 快照。

    优先从 ``WsMarketFeed`` 读取实时数据（含完整委托档位，用于深度检查）；
    若 WS 尚未就绪，则返回空数据占位（策略会因 ``is_valid=False`` 跳过该 tick）。

    ``fetch()`` 返回 ``list[(yes_data, no_data, outcome_title)]``，
    每项对应一个候选结果的数据。
    """

    def __init__(
        self,
        resolver: EventMarketResolver,
        ws_feed: Optional[WsMarketFeed] = None,
    ) -> None:
        self._resolver = resolver
        self._ws_feed = ws_feed
        self._subscribed_tokens: set[str] = set()

    async def ensure_subscribed(self) -> None:
        """确保所有子市场的 token 都已订阅到 WS feed（幂等）。"""
        if self._ws_feed is None:
            return
        info = await self._resolver.get_market_info()
        new_tokens: list[str] = []
        for market in info.markets:
            for tid in (market.yes_token_id, market.no_token_id):
                if tid not in self._subscribed_tokens:
                    new_tokens.append(tid)
        if new_tokens:
            await self._ws_feed.subscribe_tokens(*new_tokens)
            self._subscribed_tokens.update(new_tokens)
            logger.info(
                "[EventData:%s] 已订阅 %d 个新 token（共 %d 个结果）",
                self._resolver.event_slug, len(new_tokens), len(info.markets),
            )

    async def fetch(self) -> list[tuple[MarketData, MarketData, str]]:
        """
        获取所有结果子市场的当前行情快照。

        Returns
        -------
        list of (yes_data, no_data, outcome_title)
        """
        info = await self._resolver.get_market_info()

        # 确保 token 已订阅（首次调用 + resolver 缓存刷新后可能有新 token）
        await self.ensure_subscribed()

        result: list[tuple[MarketData, MarketData, str]] = []
        for market in info.markets:
            yes_data = self._build_data(
                token_id=market.yes_token_id,
                outcome="YES",
                condition_id=market.condition_id,
                gamma_price=market.yes_price,
                outcome_title=market.title,
            )
            no_data = self._build_data(
                token_id=market.no_token_id,
                outcome="NO",
                condition_id=market.condition_id,
                gamma_price=market.no_price,
                outcome_title=market.title,
            )
            result.append((yes_data, no_data, market.title))

        return result

    # ------------------------------------------------------------------ #
    # 内部构建
    # ------------------------------------------------------------------ #

    def _build_data(
        self,
        token_id: str,
        outcome: str,
        condition_id: str,
        gamma_price: Optional[float],
        outcome_title: str,
    ) -> MarketData:
        """从 WsMarketFeed 状态构建 MarketData，含完整委托档位。"""
        best_bid: Optional[float] = None
        best_ask: Optional[float] = None
        last_trade_price: Optional[float] = None
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []

        if self._ws_feed is not None and self._ws_feed.is_ready():
            state = self._ws_feed.get_snapshot(token_id)
            if state is not None:
                best_bid         = state.best_bid
                best_ask         = state.best_ask
                last_trade_price = state.last_trade_price

                # 从 WS 档位重建 bids / asks（供深度检查使用）
                # bid_levels: {price_str: size}，升序排列（最高买价在末尾）
                bids = [
                    OrderBookLevel(price=float(p), size=s)
                    for p, s in sorted(state.bid_levels.items(), key=lambda x: float(x[0]))
                    if s > 0
                ]
                # ask_levels: {price_str: size}，降序排列（最低卖价在末尾）
                asks = [
                    OrderBookLevel(price=float(p), size=s)
                    for p, s in sorted(
                        state.ask_levels.items(), key=lambda x: float(x[0]), reverse=True
                    )
                    if s > 0
                ]

        midpoint: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2
        elif best_bid is not None:
            midpoint = best_bid
        elif best_ask is not None:
            midpoint = best_ask

        spread: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        data = MarketData(
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            midpoint=midpoint,
            last_trade_price=last_trade_price,
            bids=bids,
            asks=asks,
            gamma_price=gamma_price,
        )
        return data
