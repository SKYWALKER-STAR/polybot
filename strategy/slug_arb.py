"""
通用 Slug 驱动的 Binary Split/Merge 无风险套利策略
===================================================

套利原理
--------
Polymarket 任意二元市场中，YES token + NO token 可以合并成 $1 pUSD（merge），
也可以将 $1 pUSD 拆分成 1 YES + 1 NO（split）。因此：

  YES_ask + NO_ask < $1 − min_merge_spread  →  Merge 套利
  YES_bid + NO_bid > $1 + min_split_spread  →  Split 套利

与 btc_arb / multi_arb 的区别
-------------------------------
- btc_arb   : 仅针对 BTC 5-分钟涨跌市场（UP/DOWN），由时间戳驱动切换。
- multi_arb : 专为大选等多选事件设计，每个候选结果是一个独立二元子市场，
              缺少链上 split/merge 实际调用。
- slug_arb  : 通用版本——通过 .env 中任意 slug 列表驱动，支持同时监控多个
              市场；每个 slug 可以是普通二元市场或多选事件中的子市场，包含
              完整的链上 merge_positions / split_position 调用。

配置示例（.env）
----------------
# 启用策略
SLUG_ARB_ENABLED=true
# 观察模式：仅打印，不执行真实交易（建议首次使用时开启）
SLUG_ARB_OBSERVE_MODE=true
# 目标市场 slug，逗号分隔，支持同时监控多个市场
# slug 即 Polymarket 事件/市场 URL 最后一段路径，例如：
#   https://polymarket.com/event/will-btc-reach-100k → will-btc-reach-100k
SLUG_ARB_MARKET_SLUGS=will-btc-reach-100k-2025,eth-price-end-of-2026

日志过滤
---------
每条日志均以 [slug_arb:{slug}] 为前缀，可按市场过滤：

    grep "slug_arb:will-btc-reach-100k" logs/polybot.log

执行流程
---------
Merge 套利:
  1. 并发 FOK 买入 YES @ask + NO @ask
  2. 双边全部成交 → 调用 merge_positions() 合并为 pUSD

Split 套利:
  1. split_position() 将 pUSD 拆分为 YES + NO
  2. 并发 FOK 卖出 YES @bid + NO @bid
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.client import PolymarketClient
from core.event_market_resolver import EventMarketDataService, EventMarketResolver
from core.market_data import MarketData
from core.order_manager import OrderManager, OrderRequest
from core.ws_market_feed import WsMarketFeed

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 配置
# ------------------------------------------------------------------ #

@dataclass
class SlugArbConfig:
    """套利策略运行参数（全部金额单位为 USDC）。"""

    # ---- 触发阈值 ---------------------------------------------------
    # Merge: YES_ask + NO_ask ≤ 1 - min_merge_spread
    min_merge_spread: float = 0.008

    # Split: YES_bid + NO_bid ≥ 1 + min_split_spread
    min_split_spread: float = 0.008

    # ---- 资金规模 ---------------------------------------------------
    max_trade_usdc: float = 100.0
    base_trade_usdc: float = 50.0

    # ---- 风控 -------------------------------------------------------
    # 两次套利之间的最小冷却时间（秒）
    cooldown_seconds: float = 3.0

    # 订单簿最小可用深度（shares）
    liquidity_min_size: float = 10.0

    # FOK 下单滑点保护（USDC 价格偏移）
    slippage_tolerance: float = 0.002

    # Gas 费用估算（USDC），用于净利润门槛过滤
    estimated_gas_usdc: float = 0.005

    # ---- 观察模式 ---------------------------------------------------
    # True（默认）：每 tick INFO 打印实时价格；发现机会时打印完整快照；
    #               不执行任何下单、split、merge 操作。
    # False：执行真实交易。
    observe_mode: bool = True


# ------------------------------------------------------------------ #
# 套利方向
# ------------------------------------------------------------------ #

class ArbMode(str, Enum):
    NONE  = "NONE"
    MERGE = "MERGE"   # 买 YES + 买 NO → merge → pUSD
    SPLIT = "SPLIT"   # split pUSD → 卖 YES + 卖 NO


# ------------------------------------------------------------------ #
# 套利机会快照
# ------------------------------------------------------------------ #

@dataclass
class SlugArbOpportunity:
    mode: ArbMode
    slug: str
    outcome_title: str   # 子市场/结果名称

    condition_id: str
    yes_token_id: str
    no_token_id: str

    yes_price: float   # Merge 时为 ask；Split 时为 bid
    no_price: float

    yes_outcome: str   # MarketData.outcome 原始字符串（如 "YES"）
    no_outcome: str

    trade_size_usdc: float
    gross_profit: float
    net_profit: float

    def __str__(self) -> str:
        return (
            f"SlugArbOpp({self.mode.value}  [{self.slug}:{self.outcome_title}]  "
            f"YES={self.yes_price:.4f}  NO={self.no_price:.4f}  "
            f"sum={self.yes_price + self.no_price:.4f}  "
            f"size=${self.trade_size_usdc:.2f}  net_profit=${self.net_profit:.4f})"
        )


# ------------------------------------------------------------------ #
# 单市场运行状态
# ------------------------------------------------------------------ #

@dataclass
class _MarketState:
    """
    每个 condition_id（二元子市场）的独立运行状态。
    防止并发重入、管理冷却计时、记录统计数据。
    """
    last_arb_ts: float = 0.0
    arb_in_flight: bool = False
    total_attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_net_profit: float = 0.0


# ------------------------------------------------------------------ #
# 策略主类
# ------------------------------------------------------------------ #

class SlugArbStrategy:
    """
    通用 Slug 驱动的 Binary Split/Merge 无风险套利策略。

    支持同时监控任意数量的 Polymarket 市场（由 slugs 列表指定）。
    每个 slug 可对应：
      - 普通二元市场（一对 YES/NO）
      - 多选事件（多个候选结果，每个结果是独立二元市场）

    线程安全
    --------
    所有状态通过 asyncio 单线程事件循环访问，不需要锁。

    Usage（在 bot.py 中）
    ---------------------
    ::

        strategy = SlugArbStrategy(
            client=client,
            order_manager=order_manager,
            ws_feed=ws_feed,
            slugs=["will-btc-reach-100k-2025", "eth-price-end-of-2026"],
            config=SlugArbConfig(observe_mode=True),
        )
        await strategy.on_start()
        # 每个 bot tick 中调用：
        await strategy.on_tick()
    """

    name = "slug_arb"

    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        ws_feed: WsMarketFeed,
        slugs: list[str],
        config: Optional[SlugArbConfig] = None,
    ) -> None:
        self._client = client
        self._order_manager = order_manager
        self._ws_feed = ws_feed
        self._cfg = config or SlugArbConfig()
        self._slugs = slugs

        # 每个 slug 对应独立的 Resolver + DataService（复用现有基础设施）
        # list[(slug, EventMarketDataService)]
        self._data_services: list[tuple[str, EventMarketDataService]] = []
        for slug in slugs:
            resolver = EventMarketResolver(slug, cache_ttl=300.0)
            svc = EventMarketDataService(resolver=resolver, ws_feed=ws_feed)
            self._data_services.append((slug, svc))

        # 每个 condition_id 对应独立的运行状态
        self._states: dict[str, _MarketState] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def on_start(self) -> None:
        mode_tag = "【观察模式 — 仅打印，不交易】" if self._cfg.observe_mode else "【交易模式】"
        logger.info(
            "[slug_arb] 策略启动 %s\n"
            "        监控 %d 个市场: %s\n"
            "        merge阈值=%.4f  split阈值=%.4f\n"
            "        规模=%.1f USDC  冷却=%.1fs  min流动性=%.1f shares",
            mode_tag,
            len(self._slugs),
            ", ".join(self._slugs),
            self._cfg.min_merge_spread,
            self._cfg.min_split_spread,
            self._cfg.base_trade_usdc,
            self._cfg.cooldown_seconds,
            self._cfg.liquidity_min_size,
        )
        # 预订阅所有已知市场的 token（失败时允许在首个 tick 时重试）
        for slug, svc in self._data_services:
            try:
                await svc.ensure_subscribed()
                logger.info("[slug_arb:%s] token 已订阅到 WS feed", slug)
            except Exception as exc:
                logger.warning(
                    "[slug_arb:%s] token 订阅失败，将在首个 tick 时重试: %s",
                    slug, exc,
                )

    def on_stop(self) -> None:
        total_attempts = sum(s.total_attempts for s in self._states.values())
        total_successes = sum(s.successes for s in self._states.values())
        total_failures = sum(s.failures for s in self._states.values())
        total_profit = sum(s.total_net_profit for s in self._states.values())
        logger.info(
            "[slug_arb] 策略停止 — 监控市场数=%d  总触发=%d  成功=%d  失败=%d  "
            "累计净利润≈$%.4f",
            len(self._slugs),
            total_attempts, total_successes, total_failures, total_profit,
        )
        # 逐市场打印详细统计
        for (slug, _), (cid, state) in zip(
            self._data_services,
            list(self._states.items()),
        ):
            logger.info(
                "[slug_arb:%s] 统计 — condition=%s  触发=%d  成功=%d  "
                "失败=%d  净利润≈$%.4f",
                slug, cid[:8],
                state.total_attempts, state.successes,
                state.failures, state.total_net_profit,
            )

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def on_tick(self) -> None:
        """
        遍历所有监控市场，获取最新行情并检测/执行套利机会。

        本方法由 bot.py 每 tick 调用一次；每个市场的冷却和并发控制
        均在 _process_pair() 内部独立管理，互不干扰。
        """
        for slug, svc in self._data_services:
            try:
                outcomes = await svc.fetch()
            except Exception as exc:
                logger.warning(
                    "[slug_arb:%s] 行情数据获取失败，跳过本 tick: %s", slug, exc
                )
                continue

            for yes_data, no_data, outcome_title in outcomes:
                await self._process_pair(slug, yes_data, no_data, outcome_title)

    # ------------------------------------------------------------------ #
    # 单市场处理
    # ------------------------------------------------------------------ #

    async def _process_pair(
        self,
        slug: str,
        yes_data: MarketData,
        no_data: MarketData,
        outcome_title: str,
    ) -> None:
        """
        处理一对 YES/NO token 的套利检测与执行。

        冷却、并发保护、观察模式均在此方法内处理。
        """
        condition_id = yes_data.condition_id
        state = self._states.setdefault(condition_id, _MarketState())
        label = f"slug_arb:{slug}"

        # ---- 并发保护 ------------------------------------------------
        if state.arb_in_flight:
            logger.debug(
                "[%s][%s] 套利进行中，跳过本 tick", label, outcome_title
            )
            return

        # ---- 冷却检查 ------------------------------------------------
        elapsed = time.monotonic() - state.last_arb_ts
        if elapsed < self._cfg.cooldown_seconds:
            logger.debug(
                "[%s][%s] 冷却中，还需 %.2fs",
                label, outcome_title, self._cfg.cooldown_seconds - elapsed,
            )
            return

        # ---- 数据有效性检查 ------------------------------------------
        if not yes_data.is_valid or not no_data.is_valid:
            logger.debug(
                "[%s][%s] 行情数据不完整（is_valid=False），跳过",
                label, outcome_title,
            )
            return

        # ---- 每 tick 打印实时订单簿市价 ------------------------------
        yes_ask = yes_data.best_ask
        yes_bid = yes_data.best_bid
        no_ask  = no_data.best_ask
        no_bid  = no_data.best_bid
        merge_sum = (yes_ask or 0.0) + (no_ask or 0.0)
        split_sum = (yes_bid or 0.0) + (no_bid or 0.0)

        _price_log = logger.info if self._cfg.observe_mode else logger.debug
        _price_log(
            "[%s][%s] 订单簿市价 — "
            "%s lowest_ask=%.4f  highest_bid=%.4f | "
            "%s lowest_ask=%.4f  highest_bid=%.4f | "
            "merge_sum(ask+ask)=%.4f  split_sum(bid+bid)=%.4f",
            label, outcome_title,
            yes_data.outcome, yes_ask or 0.0, yes_bid or 0.0,
            no_data.outcome,  no_ask  or 0.0, no_bid  or 0.0,
            merge_sum, split_sum,
        )

        # ---- 套利机会检测 --------------------------------------------
        opp = self._detect_opportunity(yes_data, no_data, slug, outcome_title)
        if opp is None:
            return

        # ---- 发现机会：始终以 INFO 打印完整快照 ----------------------
        observe_tag = "  【观察模式 — 不执行】" if self._cfg.observe_mode else ""
        logger.info(
            "[%s][%s] ★ 套利机会 ★  模式=%s%s\n"
            "        订单簿快照:\n"
            "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
            "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
            "          merge_sum(%s_ask+%s_ask)=%.4f  偏离$1=%.4f\n"
            "          split_sum(%s_bid+%s_bid)=%.4f  偏离$1=%.4f\n"
            "        套利规模=%.2f USDC  预期净利润=$%.4f",
            label, outcome_title, opp.mode.value, observe_tag,
            opp.yes_outcome, yes_ask or 0.0, yes_bid or 0.0,
            opp.no_outcome,  no_ask  or 0.0, no_bid  or 0.0,
            opp.yes_outcome, opp.no_outcome, merge_sum, 1.0 - merge_sum,
            opp.yes_outcome, opp.no_outcome, split_sum, split_sum - 1.0,
            opp.trade_size_usdc, opp.net_profit,
        )

        # 观察模式：仅打印，不执行任何交易操作
        if self._cfg.observe_mode:
            return

        # ---- 执行套利 ------------------------------------------------
        state.arb_in_flight = True
        state.total_attempts += 1
        try:
            if opp.mode == ArbMode.MERGE:
                await self._execute_merge_arb(opp, label)
            else:
                await self._execute_split_arb(opp, label)
            state.successes += 1
            state.total_net_profit += opp.net_profit
        except Exception as exc:
            state.failures += 1
            logger.exception(
                "[%s][%s] 套利执行异常: %s", label, outcome_title, exc
            )
        finally:
            state.arb_in_flight = False
            state.last_arb_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    # 机会检测
    # ------------------------------------------------------------------ #

    def _detect_opportunity(
        self,
        yes_data: MarketData,
        no_data: MarketData,
        slug: str,
        outcome_title: str,
    ) -> Optional[SlugArbOpportunity]:
        """
        扫描订单簿，识别 Merge 或 Split 套利机会。

        Merge: YES_ask + NO_ask < 1 − min_merge_spread
        Split: YES_bid + NO_bid > 1 + min_split_spread
        """
        label = f"slug_arb:{slug}"
        yes_ask = yes_data.best_ask
        no_ask  = no_data.best_ask
        yes_bid = yes_data.best_bid
        no_bid  = no_data.best_bid

        # ---- Merge 套利 -------------------------------------------
        if yes_ask is not None and no_ask is not None:
            ask_sum = yes_ask + no_ask
            gross_per_unit = 1.0 - ask_sum

            yes_ask_depth = self._get_ask_depth(yes_data, yes_ask)
            no_ask_depth  = self._get_ask_depth(no_data, no_ask)

            if gross_per_unit > self._cfg.min_merge_spread:
                if (
                    yes_ask_depth >= self._cfg.liquidity_min_size
                    and no_ask_depth >= self._cfg.liquidity_min_size
                ):
                    trade_usdc = min(
                        self._cfg.base_trade_usdc,
                        self._cfg.max_trade_usdc,
                        yes_ask_depth * yes_ask,
                        no_ask_depth  * no_ask,
                    )
                    gross_profit = trade_usdc * gross_per_unit / ask_sum
                    net_profit   = gross_profit - self._cfg.estimated_gas_usdc

                    if net_profit > 0:
                        return SlugArbOpportunity(
                            mode=ArbMode.MERGE,
                            slug=slug,
                            outcome_title=outcome_title,
                            condition_id=yes_data.condition_id,
                            yes_token_id=yes_data.token_id,
                            no_token_id=no_data.token_id,
                            yes_price=yes_ask,
                            no_price=no_ask,
                            yes_outcome=yes_data.outcome,
                            no_outcome=no_data.outcome,
                            trade_size_usdc=trade_usdc,
                            gross_profit=gross_profit,
                            net_profit=net_profit,
                        )
                else:
                    logger.info(
                        "[%s][%s] Merge 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        label, outcome_title, ask_sum,
                        yes_data.outcome, yes_ask_depth,
                        no_data.outcome,  no_ask_depth,
                        self._cfg.liquidity_min_size,
                    )

        # ---- Split 套利 -------------------------------------------
        if yes_bid is not None and no_bid is not None:
            bid_sum = yes_bid + no_bid
            gross_per_unit = bid_sum - 1.0

            yes_bid_depth = self._get_bid_depth(yes_data, yes_bid)
            no_bid_depth  = self._get_bid_depth(no_data, no_bid)

            if gross_per_unit > self._cfg.min_split_spread:
                if (
                    yes_bid_depth >= self._cfg.liquidity_min_size
                    and no_bid_depth >= self._cfg.liquidity_min_size
                ):
                    trade_usdc = min(
                        self._cfg.base_trade_usdc,
                        self._cfg.max_trade_usdc,
                        yes_bid_depth * yes_bid,
                        no_bid_depth  * no_bid,
                    )
                    gross_profit = trade_usdc * gross_per_unit
                    net_profit   = gross_profit - self._cfg.estimated_gas_usdc

                    if net_profit > 0:
                        return SlugArbOpportunity(
                            mode=ArbMode.SPLIT,
                            slug=slug,
                            outcome_title=outcome_title,
                            condition_id=yes_data.condition_id,
                            yes_token_id=yes_data.token_id,
                            no_token_id=no_data.token_id,
                            yes_price=yes_bid,
                            no_price=no_bid,
                            yes_outcome=yes_data.outcome,
                            no_outcome=no_data.outcome,
                            trade_size_usdc=trade_usdc,
                            gross_profit=gross_profit,
                            net_profit=net_profit,
                        )
                else:
                    logger.info(
                        "[%s][%s] Split 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        label, outcome_title, bid_sum,
                        yes_data.outcome, yes_bid_depth,
                        no_data.outcome,  no_bid_depth,
                        self._cfg.liquidity_min_size,
                    )

        return None

    # ------------------------------------------------------------------ #
    # Merge 套利执行
    # ------------------------------------------------------------------ #

    async def _execute_merge_arb(
        self, opp: SlugArbOpportunity, label: str
    ) -> None:
        """
        步骤：
          1. 并发 FOK 买入 YES @ask + NO @ask
          2. 双边均成交 → merge_positions() 合并为 pUSD
          3. 任一边未成交 → 警告日志（需人工处理残余头寸）
        """
        t0 = time.perf_counter()

        half_usdc  = opp.trade_size_usdc / 2.0
        yes_shares = round(half_usdc / opp.yes_price, 4)
        no_shares  = round(half_usdc / opp.no_price,  4)

        yes_req = OrderRequest(
            token_id=opp.yes_token_id,
            condition_id=opp.condition_id,
            outcome=opp.yes_outcome,
            side="BUY",
            size=half_usdc,
            price=round(min(0.99, opp.yes_price + self._cfg.slippage_tolerance), 4),
            order_type="FOK",
            strategy_tag=f"{label}_merge_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="BUY",
            size=half_usdc,
            price=round(min(0.99, opp.no_price + self._cfg.slippage_tolerance), 4),
            order_type="FOK",
            strategy_tag=f"{label}_merge_no",
        )

        logger.info(
            "[%s][%s] MERGE — 并发买入 %s %.4f shares @%.4f + %s %.4f shares @%.4f  "
            "总规模=%.2f USDC  预期净利=$%.4f",
            label, opp.outcome_title,
            opp.yes_outcome, yes_shares, yes_req.price,
            opp.no_outcome,  no_shares,  no_req.price,
            opp.trade_size_usdc, opp.net_profit,
        )

        # 并发提交双边买单（同时发出，最小化价格漂移风险）
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )
        buy_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "[%s][%s] MERGE 买单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            label, opp.outcome_title,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            buy_ms,
        )

        if yes_result.success and no_result.success:
            merge_amount = min(yes_shares, no_shares)
            await self._do_merge(
                opp.condition_id, merge_amount, opp.net_profit, label, opp.outcome_title
            )
        elif yes_result.success and not no_result.success:
            logger.error(
                "[%s][%s] MERGE 风险！%s 成交但 %s 未成交 — 需人工平仓 %s 头寸",
                label, opp.outcome_title,
                opp.yes_outcome, opp.no_outcome, opp.yes_outcome,
            )
            raise RuntimeError("MERGE 单边成交，需人工处理")
        elif not yes_result.success and no_result.success:
            logger.error(
                "[%s][%s] MERGE 风险！%s 成交但 %s 未成交 — 需人工平仓 %s 头寸",
                label, opp.outcome_title,
                opp.no_outcome, opp.yes_outcome, opp.no_outcome,
            )
            raise RuntimeError("MERGE 单边成交，需人工处理")
        else:
            logger.warning(
                "[%s][%s] MERGE 双边均未成交，无头寸风险。",
                label, opp.outcome_title,
            )
            raise RuntimeError("MERGE 双边均未成交")

    async def _do_merge(
        self,
        condition_id: str,
        amount: float,
        expected_net_profit: float,
        label: str,
        outcome_title: str,
    ) -> None:
        """调用 SDK merge_positions，将 YES+NO 合并为 pUSD。"""
        t0 = time.perf_counter()
        result = await self._client.merge_positions(
            condition_id=condition_id,
            amount=amount,
        )
        merge_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[%s][%s] ✅ MERGE 完成 — tx=%s  耗时=%.0fms  预期净利=$%.4f",
            label, outcome_title,
            result.get("transaction_hash", ""),
            merge_ms,
            expected_net_profit,
        )

    # ------------------------------------------------------------------ #
    # Split 套利执行
    # ------------------------------------------------------------------ #

    async def _execute_split_arb(
        self, opp: SlugArbOpportunity, label: str
    ) -> None:
        """
        步骤：
          1. split_position() 将 pUSD 拆成等量 YES + NO
          2. 并发 FOK 卖出 YES @bid + NO @bid
          3. 记录利润
        """
        t0 = time.perf_counter()
        split_amount = opp.trade_size_usdc  # pUSD 面值 = 拆出的 shares 数量

        logger.info(
            "[%s][%s] SPLIT — 拆分 %.2f pUSD  %s_bid=%.4f  %s_bid=%.4f  "
            "预期净利=$%.4f",
            label, opp.outcome_title,
            split_amount,
            opp.yes_outcome, opp.yes_price,
            opp.no_outcome,  opp.no_price,
            opp.net_profit,
        )

        # Step 1: 链上 split
        split_result = await self._client.split_position(
            condition_id=opp.condition_id,
            amount=split_amount,
        )
        split_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[%s][%s] SPLIT tx 完成 — tx=%s  耗时=%.0fms",
            label, opp.outcome_title,
            split_result.get("transaction_hash", ""),
            split_ms,
        )

        # Step 2: 并发 FOK 卖出双边（bid 减去滑点容忍，保证成交概率）
        yes_sell_price = round(max(0.01, opp.yes_price - self._cfg.slippage_tolerance), 4)
        no_sell_price  = round(max(0.01, opp.no_price  - self._cfg.slippage_tolerance), 4)

        yes_req = OrderRequest(
            token_id=opp.yes_token_id,
            condition_id=opp.condition_id,
            outcome=opp.yes_outcome,
            side="SELL",
            size=split_amount * opp.yes_price,  # USDC 价值
            price=yes_sell_price,
            order_type="FOK",
            strategy_tag=f"{label}_split_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="SELL",
            size=split_amount * opp.no_price,
            price=no_sell_price,
            order_type="FOK",
            strategy_tag=f"{label}_split_no",
        )

        t1 = time.perf_counter()
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )
        sell_ms = (time.perf_counter() - t1) * 1000

        logger.info(
            "[%s][%s] SPLIT 卖单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            label, opp.outcome_title,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            sell_ms,
        )

        if yes_result.success and no_result.success:
            total_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[%s][%s] ✅ SPLIT 套利完成 — 总耗时=%.0fms  预期净利=$%.4f",
                label, opp.outcome_title, total_ms, opp.net_profit,
            )
        else:
            logger.error(
                "[%s][%s] SPLIT 卖单未完全成交 — %s ok=%s  %s ok=%s  "
                "已拆分头寸可能需要人工处理",
                label, opp.outcome_title,
                opp.yes_outcome, yes_result.success,
                opp.no_outcome,  no_result.success,
            )
            raise RuntimeError("SPLIT 卖单未完全成交，需人工处理")

    # ------------------------------------------------------------------ #
    # 订单簿深度辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_ask_depth(data: MarketData, price: float) -> float:
        """返回 best ask 价格层的可用 shares（用于流动性检查）。"""
        for level in data.asks:
            if abs(level.price - price) < 1e-6:
                return level.size
        return 0.0

    @staticmethod
    def _get_bid_depth(data: MarketData, price: float) -> float:
        """返回 best bid 价格层的可用 shares（用于流动性检查）。"""
        for level in data.bids:
            if abs(level.price - price) < 1e-6:
                return level.size
        return 0.0
