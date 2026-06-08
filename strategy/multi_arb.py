"""
多选市场 Binary Merge/Split 套利策略（Multi-outcome Arbitrage）
==============================================================

套利原理
--------
Polymarket 多选事件（如大选、颁奖礼）中，每个候选结果对应一个独立的二元市场
（YES = 该候选人赢，NO = 该候选人不赢）。与 BTC 5分钟市场完全相同，每个二元
市场都支持 YES+NO → $1 的合并（merge）和 $1 → YES+NO 的拆分（split）操作。

因此，对于每个候选结果，本策略独立检测 Merge 或 Split 套利机会：

    YES_ask + NO_ask < $1.00 − min_merge_spread
        → Merge 套利：买入双边，合并为 pUSD

    YES_bid + NO_bid > $1.00 + min_split_spread
        → Split 套利：拆分 pUSD，在双边卖出

此外，本策略还会在 INFO 级记录所有候选结果的 YES ask 价格之和，方便用户
观察多选市场整体定价偏差。

日志标识
--------
所有日志均以 ``[multi_arb:{event_slug}]`` 为前缀，可通过 grep 快速过滤特定市场：

    grep "multi_arb:democratic-presidential-nominee-2028" logs/polybot.log

选择要监听的市场
----------------
在 .env 中设置（也是唯一需要修改的地方）：

    ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028
    ELECTION_ARB_ENABLED=true
    ELECTION_ARB_OBSERVE_MODE=true   # 建议先用观察模式验证
    # 同时监听多个市场：
    # ELECTION_MARKET_SLUGS=slug1,slug2,slug3

默认行为
--------
- 观察模式（observe_mode）默认启用：每次发现机会时以 INFO 打印详情，不下单
- 若关闭 observe_mode，策略将通过 OrderManager 执行真实交易
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.market_data import MarketData
from core.order_manager import OrderManager, OrderRequest

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 配置
# ------------------------------------------------------------------ #

@dataclass
class MultiArbConfig:
    """多选市场套利策略运行参数（金额单位：USDC）。"""

    # ---- 触发阈值 -----------------------------------------------
    # Merge 触发：YES_ask + NO_ask ≤ 1 - min_merge_spread
    min_merge_spread: float = 0.005

    # Split 触发：YES_bid + NO_bid ≥ 1 + min_split_spread
    min_split_spread: float = 0.005

    # ---- 资金规模 -----------------------------------------------
    max_trade_usdc: float = 100.0
    base_trade_usdc: float = 20.0

    # ---- 风控 ---------------------------------------------------
    cooldown_seconds: float = 5.0
    liquidity_min_size: float = 5.0
    slippage_tolerance: float = 0.002
    estimated_gas_usdc: float = 0.005

    # ---- 观察模式 -----------------------------------------------
    # True（默认）：每 tick 以 INFO 打印实时价格；发现机会时打印完整快照；
    #              不执行任何下单、split、merge 操作。
    # False：实际执行套利交易。
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
class MultiArbOpportunity:
    mode: ArbMode
    outcome_title: str    # 候选结果名称，如 "Kamala Harris"
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    # MarketData.outcome 的原始字符串（通常为 "YES"/"NO"）
    yes_outcome: str
    no_outcome: str
    trade_size_usdc: float
    gross_profit: float
    net_profit: float

    def __str__(self) -> str:
        return (
            f"MultiArbOpp({self.mode.value}  [{self.outcome_title}]  "
            f"YES={self.yes_price:.4f}  NO={self.no_price:.4f}  "
            f"sum={self.yes_price + self.no_price:.4f}  "
            f"size=${self.trade_size_usdc:.2f}  net_profit=${self.net_profit:.4f})"
        )


# ------------------------------------------------------------------ #
# 统计
# ------------------------------------------------------------------ #

@dataclass
class _ArbStats:
    total_attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_net_profit: float = 0.0


# ------------------------------------------------------------------ #
# 策略主类
# ------------------------------------------------------------------ #

class MultiArbStrategy:
    """
    多选市场每个候选结果的独立 binary merge/split 套利策略。

    ``on_tick`` 接收来自 ``EventMarketDataService.fetch()`` 的数据列表，
    格式为 ``list[(yes_data, no_data, outcome_title)]``，
    对每个结果市场独立执行套利检测和（非观察模式下）执行。

    线程安全
    --------
    所有状态通过 asyncio 单线程事件循环访问，不需要锁。
    """

    def __init__(
        self,
        event_slug: str,
        order_manager: OrderManager,
        config: Optional[MultiArbConfig] = None,
    ) -> None:
        self._slug = event_slug
        self._label = f"multi_arb:{event_slug}"
        self._order_manager = order_manager
        self._cfg = config or MultiArbConfig()

        self._last_arb_ts: float = 0.0
        self._arb_in_flight: bool = False
        self._stats = _ArbStats()

    @property
    def name(self) -> str:
        return self._label

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        mode_tag = "【观察模式 — 仅打印，不交易】" if self._cfg.observe_mode else "【交易模式】"
        logger.info(
            "[%s] 多选市场套利策略启动 %s\n"
            "        事件slug=%s  merge阈值=%.4f  split阈值=%.4f\n"
            "        规模=%.1f USDC  冷却=%.1fs  min流动性=%.1f shares",
            self._label, mode_tag,
            self._slug,
            self._cfg.min_merge_spread,
            self._cfg.min_split_spread,
            self._cfg.base_trade_usdc,
            self._cfg.cooldown_seconds,
            self._cfg.liquidity_min_size,
        )

    def on_stop(self) -> None:
        logger.info(
            "[%s] 多选市场套利策略停止 — 总触发=%d  成功=%d  失败=%d  累计净利润≈$%.4f",
            self._label,
            self._stats.total_attempts,
            self._stats.successes,
            self._stats.failures,
            self._stats.total_net_profit,
        )

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    async def on_tick(
        self,
        outcomes: list[tuple[MarketData, MarketData, str]],
    ) -> None:
        """
        Parameters
        ----------
        outcomes : list of (yes_data, no_data, outcome_title)
            来自 ``EventMarketDataService.fetch()`` 的数据，每项对应一个结果。
        """
        if not outcomes:
            return

        # --- 跨结果 YES ask/bid 价格汇总（信息性，DEBUG 级） ---
        yes_asks = [y.best_ask for y, _, _ in outcomes if y.best_ask is not None]
        yes_bids = [y.best_bid for y, _, _ in outcomes if y.best_bid is not None]
        ask_sum = sum(yes_asks)
        bid_sum = sum(yes_bids)

        #_log = logger.info if self._cfg.observe_mode else logger.debug
        _log = logger.info
        _log(
            "[%s] 跨结果价格 — "
            "YES ask之和=%.4f (偏离$1=%.4f)  "
            "YES bid之和=%.4f (偏离$1=%.4f)  "
            "结果数=%d",
            self._label,
            ask_sum, 1.0 - ask_sum,
            bid_sum, bid_sum - 1.0,
            len(outcomes),
        )

        # --- 逐结果 binary arb 检测 ---
        for yes_data, no_data, outcome_title in outcomes:
            if not yes_data.is_valid or not no_data.is_valid:
                logger.debug(
                    "[%s][%s] 行情数据不完整，跳过",
                    self._label, outcome_title,
                )
                continue

            yes_ask = yes_data.best_ask
            yes_bid = yes_data.best_bid
            no_ask  = no_data.best_ask
            no_bid  = no_data.best_bid
            merge_sum = (yes_ask or 0.0) + (no_ask or 0.0)
            split_sum = (yes_bid or 0.0) + (no_bid or 0.0)

            _log(
                "[%s][%s] "
                "%s lowest_ask=%.4f  highest_bid=%.4f | "
                "%s lowest_ask=%.4f  highest_bid=%.4f | "
                "merge_sum=%.4f  split_sum=%.4f",
                self._label, outcome_title,
                yes_data.outcome, yes_ask or 0.0, yes_bid or 0.0,
                no_data.outcome,  no_ask  or 0.0, no_bid  or 0.0,
                merge_sum, split_sum,
            )

            opp = self._detect_opportunity(yes_data, no_data, outcome_title)
            if opp is None:
                continue

            # ---- 发现套利机会，始终以 INFO 打印完整快照 ----
            observe_tag = "  【观察模式 — 不执行】" if self._cfg.observe_mode else ""
            logger.info(
                "[%s][%s] ★ 套利机会 ★  模式=%s%s\n"
                "        订单簿快照:\n"
                "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
                "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
                "          merge_sum(%s_ask+%s_ask)=%.4f  偏离$1=%.4f\n"
                "          split_sum(%s_bid+%s_bid)=%.4f  偏离$1=%.4f\n"
                "        套利规模=%.2f USDC  预期净利润=$%.4f",
                self._label, outcome_title, opp.mode.value, observe_tag,
                opp.yes_outcome, yes_ask or 0.0, yes_bid or 0.0,
                opp.no_outcome,  no_ask  or 0.0, no_bid  or 0.0,
                opp.yes_outcome, opp.no_outcome, merge_sum, 1.0 - merge_sum,
                opp.yes_outcome, opp.no_outcome, split_sum, split_sum - 1.0,
                opp.trade_size_usdc, opp.net_profit,
            )

            if self._cfg.observe_mode:
                continue

            # ---- 执行套利（交易模式）----
            if self._arb_in_flight:
                logger.debug("[%s] 套利进行中，跳过本 tick", self._label)
                continue

            now = time.monotonic()
            if now - self._last_arb_ts < self._cfg.cooldown_seconds:
                remaining = self._cfg.cooldown_seconds - (now - self._last_arb_ts)
                logger.debug(
                    "[%s] 冷却中，%.1fs 后可再次触发", self._label, remaining
                )
                continue

            self._arb_in_flight = True
            self._stats.total_attempts += 1
            try:
                if opp.mode == ArbMode.MERGE:
                    await self._execute_merge_arb(opp)
                else:
                    await self._execute_split_arb(opp)
                self._stats.successes += 1
                self._stats.total_net_profit += opp.net_profit
            except Exception as exc:
                self._stats.failures += 1
                logger.exception(
                    "[%s][%s] 套利执行异常: %s", self._label, outcome_title, exc
                )
            finally:
                self._arb_in_flight = False
                self._last_arb_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    # 机会检测
    # ------------------------------------------------------------------ #

    def _detect_opportunity(
        self,
        yes_data: MarketData,
        no_data: MarketData,
        outcome_title: str,
    ) -> Optional[MultiArbOpportunity]:
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
                        return MultiArbOpportunity(
                            mode=ArbMode.MERGE,
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
                    logger.debug(
                        "[%s][%s] Merge 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        self._label, outcome_title, ask_sum,
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
                        return MultiArbOpportunity(
                            mode=ArbMode.SPLIT,
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
                    logger.debug(
                        "[%s][%s] Split 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        self._label, outcome_title, bid_sum,
                        yes_data.outcome, yes_bid_depth,
                        no_data.outcome,  no_bid_depth,
                        self._cfg.liquidity_min_size,
                    )

        return None

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_ask_depth(data: MarketData, price: float) -> float:
        """返回 best ask 价格层的可用 shares。"""
        for level in data.asks:
            if abs(level.price - price) < 1e-6:
                return level.size
        return 0.0

    @staticmethod
    def _get_bid_depth(data: MarketData, price: float) -> float:
        """返回 best bid 价格层的可用 shares。"""
        for level in data.bids:
            if abs(level.price - price) < 1e-6:
                return level.size
        return 0.0

    # ------------------------------------------------------------------ #
    # 执行 — Merge
    # ------------------------------------------------------------------ #

    async def _execute_merge_arb(self, opp: MultiArbOpportunity) -> None:
        """并发 FOK 买入 YES 和 NO，成功后执行链上 merge。"""
        half_usdc = opp.trade_size_usdc / 2.0
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
            strategy_tag=f"{self._label}_merge_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="BUY",
            size=half_usdc,
            price=round(min(0.99, opp.no_price + self._cfg.slippage_tolerance), 4),
            order_type="FOK",
            strategy_tag=f"{self._label}_merge_no",
        )

        logger.info(
            "[%s][%s] MERGE — 并发买入 %s %.4f shares @%.4f + %s %.4f shares @%.4f  "
            "总规模=%.2f USDC  预期净利=$%.4f",
            self._label, opp.outcome_title,
            opp.yes_outcome, yes_shares, yes_req.price,
            opp.no_outcome,  no_shares,  no_req.price,
            opp.trade_size_usdc, opp.net_profit,
        )

        t0 = time.perf_counter()
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "[%s][%s] MERGE 买单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            self._label, opp.outcome_title,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            elapsed_ms,
        )

        if not yes_result.success or not no_result.success:
            logger.error(
                "[%s][%s] MERGE 买单未全部成交 (%s=%s %s=%s)，跳过 merge 操作。"
                "如有单边头寸请人工处理。",
                self._label, opp.outcome_title,
                opp.yes_outcome, yes_result.success,
                opp.no_outcome,  no_result.success,
            )
            self._stats.failures += 1

    # ------------------------------------------------------------------ #
    # 执行 — Split
    # ------------------------------------------------------------------ #

    async def _execute_split_arb(self, opp: MultiArbOpportunity) -> None:
        """并发 FOK 卖出 YES 和 NO，假设已持有通过 split 获得的 token。"""
        yes_sell_price = round(max(0.01, opp.yes_price - self._cfg.slippage_tolerance), 4)
        no_sell_price  = round(max(0.01, opp.no_price  - self._cfg.slippage_tolerance), 4)

        yes_req = OrderRequest(
            token_id=opp.yes_token_id,
            condition_id=opp.condition_id,
            outcome=opp.yes_outcome,
            side="SELL",
            size=opp.trade_size_usdc * opp.yes_price,
            price=yes_sell_price,
            order_type="FOK",
            strategy_tag=f"{self._label}_split_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="SELL",
            size=opp.trade_size_usdc * opp.no_price,
            price=no_sell_price,
            order_type="FOK",
            strategy_tag=f"{self._label}_split_no",
        )

        logger.info(
            "[%s][%s] SPLIT — 并发卖出 %s @%.4f + %s @%.4f  "
            "总规模=%.2f USDC  预期净利=$%.4f",
            self._label, opp.outcome_title,
            opp.yes_outcome, yes_sell_price,
            opp.no_outcome,  no_sell_price,
            opp.trade_size_usdc, opp.net_profit,
        )

        t0 = time.perf_counter()
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "[%s][%s] SPLIT 卖单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            self._label, opp.outcome_title,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            elapsed_ms,
        )
