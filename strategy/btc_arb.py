"""
BTC 5分钟无风险价差套利策略（Split/Merge Arbitrage）
=====================================================

套利原理
--------
Polymarket 二元市场中，YES token + NO token 理论上永远可以被合并（merge）成 $1 的 pUSD，
也可以将 $1 pUSD 拆分（split）成 1 个 YES + 1 个 NO token。
因此：

  YES_ask + NO_ask < $1.00  →  「Merge 套利」（买入双边后合并）
    - 花费: (YES_ask + NO_ask) × N
    - 合并后得到: N pUSD
    - 无风险利润: N × (1 - YES_ask - NO_ask) - gas

  YES_ask + NO_ask > $1.00  →  「Split 套利」（拆分后在双边卖出）
    - 花费: N pUSD（拆分）
    - 卖出双边得到: N × (YES_bid + NO_bid)
    - 无风险利润: N × (YES_bid + NO_bid - 1) - gas

两种套利都与 BTC 涨跌结果无关，属于纯无风险价差交易。

执行流程（毫秒级）
------------------
Merge 套利:
  1. 同时并发提交 FOK 买单：BUY YES @ask, BUY NO @ask
  2. 等待双边成交确认
  3. 调用 merge_positions() 合并成 pUSD

Split 套利:
  1. 调用 split_position() 将 pUSD 拆分
  2. 同时并发提交 FOK 卖单：SELL YES @bid, SELL NO @bid

风控机制
--------
- min_spread_usdc: 最低净利润阈值（扣除 gas 估算后）
- max_trade_usdc: 单次套利最大 USDC 规模
- cooldown_seconds: 两次套利之间的冷却期（防止链上 tx 堆积）
- max_concurrent_arbs: 最大并发套利次数（避免资金超用）
- liquidity_min_size: 订单簿深度要求（ask/bid 层必须有足够数量）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.client import PolymarketClient
from core.market_data import MarketData
from core.order_manager import OrderManager, OrderRequest, OrderResult
from audit.logger import AuditLogger
from database.models import AuditAction, AuditResult
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 配置
# ------------------------------------------------------------------ #

@dataclass
class ArbConfig:
    """套利策略运行参数（全部金额单位为 USDC）。"""

    # ---- 触发条件 ---------------------------------------------------
    # Merge 套利触发阈值：YES_ask + NO_ask ≤ 1 - min_merge_spread
    # 例如 0.008 表示总价差至少 0.8 分才触发（扣除 gas 后仍有利润）
    min_merge_spread: float = 0.008

    # Split 套利触发阈值：YES_bid + NO_bid ≥ 1 + min_split_spread
    min_split_spread: float = 0.008

    # ---- 资金规模 ---------------------------------------------------
    # 单次套利最大 USDC 规模
    max_trade_usdc: float = 100.0

    # 每次套利的基础交易量（USDC），实际数量受深度限制
    base_trade_usdc: float = 50.0

    # ---- 风控 -------------------------------------------------------
    # 两次套利之间的最小冷却时间（秒）
    # 主要用于等待链上 merge/split tx 确认，防止 nonce 冲突
    cooldown_seconds: float = 3.0

    # 订单簿最小可用深度（shares），低于此认为流动性不足
    liquidity_min_size: float = 10.0

    # 价格滑点保护：FOK 买单实际价格最多高于 ask 多少（0.002 = 0.2 分）
    slippage_tolerance: float = 0.002

    # Gas 费用估算（USDC），用于净利润计算；Polygon 上通常 < $0.01
    estimated_gas_usdc: float = 0.005

    # ---- 观察模式 ---------------------------------------------------
    # 设为 True 时：每 tick 以 INFO 级打印实时订单簿市价，发现机会时打印完整快照，
    # 但跳过所有下单、split、merge 操作。适合上线前的市场观察与阈值调优。
    observe_mode: bool = False


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
class ArbOpportunity:
    mode: ArbMode
    condition_id: str

    yes_token_id: str
    no_token_id: str

    yes_price: float   # 买入用 ask；卖出用 bid
    no_price: float

    # MarketData.outcome 的原始字符串（如 "UP"/"DOWN" 或 "YES"/"NO"）
    # 直接从传入的 MarketData 读取，避免硬编码
    yes_outcome: str
    no_outcome: str

    trade_size_usdc: float   # 本次套利规模（USDC）
    gross_profit: float      # 税前利润（USDC）
    net_profit: float        # 净利润（扣除估算 gas）

    def __str__(self) -> str:
        return (
            f"ArbOpp({self.mode.value}  "
            f"YES={self.yes_price:.4f}  NO={self.no_price:.4f}  "
            f"sum={self.yes_price + self.no_price:.4f}  "
            f"size=${self.trade_size_usdc:.2f}  "
            f"net_profit=${self.net_profit:.4f})"
        )


# ------------------------------------------------------------------ #
# 策略主类
# ------------------------------------------------------------------ #

class BtcArbStrategy(BaseStrategy):
    """
    BTC 5分钟 YES/NO 价差无风险套利策略。

    该策略实现 BaseStrategy 接口，但为了执行链上 split/merge，需要直接访问
    PolymarketClient，并由 bot.py 的套利流程驱动。

    线程安全
    --------
    所有状态通过 asyncio 单线程事件循环访问，不需要锁。
    """

    name = "btc_arb"

    def __init__(
        self,
        config: Optional[ArbConfig] = None,
    ) -> None:
        self._cfg = config or ArbConfig()
        self._audit = AuditLogger()
        # infra 依赖通过 bind() 注入
        self._client = None
        self._order_manager = None
        self._market_data_service = None

        self._last_arb_ts: float = 0.0
        self._arb_in_flight: bool = False
        self._stats = _ArbStats()

    def bind(self, *, client=None, order_manager=None, market_data_service=None, **kwargs) -> None:
        """注入基础设施依赖（由 bot 在 start() 后统一调用）。"""
        if client is not None:
            self._client = client
        if order_manager is not None:
            self._order_manager = order_manager
        if market_data_service is not None:
            self._market_data_service = market_data_service

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def on_start(self) -> None:
        mode_tag = "【观察模式 — 仅打印，不交易】" if self._cfg.observe_mode else "【交易模式】"
        logger.info(
            "[%s] 套利策略启动 %s — merge阈值=%.4f  split阈值=%.4f  "
            "规模=%.1f USDC  冷却=%.1fs  min流动性=%.1f shares",
            self.name, mode_tag,
            self._cfg.min_merge_spread,
            self._cfg.min_split_spread,
            self._cfg.base_trade_usdc,
            self._cfg.cooldown_seconds,
            self._cfg.liquidity_min_size,
        )

    def on_stop(self) -> None:
        logger.info(
            "[%s] 套利策略停止 — 总触发=%d  成功=%d  失败=%d  累计净利润≈$%.4f",
            self.name,
            self._stats.total_attempts,
            self._stats.successes,
            self._stats.failures,
            self._stats.total_net_profit,
        )

    # ------------------------------------------------------------------ #
    # 主入口（每次 tick 由 ArbBot 调用）
    # ------------------------------------------------------------------ #

    async def on_tick(self) -> list:
        """
        自行获取行情，检测套利机会并立即异步执行。
        本方法设计为非阻塞快速返回：若已有套利在途或仍在冷却期则立即跳过。
        套利类策略内部自行下单，不向 bot 返回 OrderRequest 列表。
        """
        if self._market_data_service is None:
            logger.error("[%s] MarketDataService 未绑定，跳过本次 tick。", self.name)
            return []
        try:
            yes_data, no_data = await self._market_data_service.fetch()
        except Exception as exc:
            logger.error("[%s] 行情数据获取失败，跳过本次 tick: %s", self.name, exc)
            return []

        if self._arb_in_flight:
            logger.debug("[%s] 套利已在途，跳过本 tick。", self.name)
            return

        elapsed = time.monotonic() - self._last_arb_ts
        if elapsed < self._cfg.cooldown_seconds:
            logger.debug(
                "[%s] 冷却中，还需 %.2fs。", self.name,
                self._cfg.cooldown_seconds - elapsed,
            )
            return

        if not yes_data.is_valid or not no_data.is_valid:
            logger.warning("[%s] 行情数据不完整，跳过。", self.name)
            return

        # ---- 每 tick 打印实时订单簿市价 ----
        # 观察模式：INFO 级（每 tick 可见）；交易模式：DEBUG 级（不污染生产日志）
        yes_ask = yes_data.best_ask
        yes_bid = yes_data.best_bid
        no_ask  = no_data.best_ask
        no_bid  = no_data.best_bid
        merge_sum = (yes_ask or 0.0) + (no_ask or 0.0)
        split_sum = (yes_bid or 0.0) + (no_bid or 0.0)
        logger.debug(
            "[%s] 订单簿市价 — "
            "%s lowest_ask=%.4f  highest_bid=%.4f | "
            "%s lowest_ask=%.4f  highest_bid=%.4f | "
            "merge_sum(ask+ask)=%.4f  split_sum(bid+bid)=%.4f",
            self.name,
            yes_data.outcome, yes_ask or 0.0, yes_bid or 0.0,
            no_data.outcome,  no_ask  or 0.0, no_bid  or 0.0,
            merge_sum, split_sum,
        )

        opp = self._detect_opportunity(yes_data, no_data)
        if opp is None:
            return []

        # ---- 套利机会：始终以 INFO 级完整打印触发时刻的订单簿快照 ----
        observe_tag = "  【观察模式 — 不执行】" if self._cfg.observe_mode else ""
        logger.info(
            "[%s] ★ 套利机会 ★  模式=%s%s\n"
            "        订单簿快照:\n"
            "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
            "          %s  lowest_ask(买入价)=%.4f   highest_bid(卖出价)=%.4f\n"
            "          merge_sum(%s_ask+%s_ask)=%.4f  偏离$1=%.4f\n"
            "          split_sum(%s_bid+%s_bid)=%.4f  偏离$1=%.4f\n"
            "        套利规模=%.2f USDC  预期净利润=$%.4f",
            self.name, opp.mode.value, observe_tag,
            opp.yes_outcome, yes_ask or 0.0, yes_bid or 0.0,
            opp.no_outcome,  no_ask  or 0.0, no_bid  or 0.0,
            opp.yes_outcome, opp.no_outcome, merge_sum, 1.0 - merge_sum,
            opp.yes_outcome, opp.no_outcome, split_sum, split_sum - 1.0,
            opp.trade_size_usdc, opp.net_profit,
        )

        self._audit.record(
            action=AuditAction.ARB_OPPORTUNITY,
            result=AuditResult.SUCCESS,
            details={
                "strategy": self.name,
                "mode": opp.mode.value,
                "condition_id": opp.condition_id,
                "yes_token_id": opp.yes_token_id,
                "no_token_id": opp.no_token_id,
                "yes_outcome": opp.yes_outcome,
                "no_outcome": opp.no_outcome,
                "yes_best_ask": yes_ask,
                "yes_best_bid": yes_bid,
                "no_best_ask": no_ask,
                "no_best_bid": no_bid,
                "merge_sum": merge_sum,
                "split_sum": split_sum,
                "merge_deviation": 1.0 - merge_sum,
                "split_deviation": split_sum - 1.0,
                "trade_size_usdc": opp.trade_size_usdc,
                "gross_profit": opp.gross_profit,
                "net_profit": opp.net_profit,
                "observe_mode": self._cfg.observe_mode,
            },
        )

        # 观察模式：仅打印，不执行任何交易操作
        if self._cfg.observe_mode:
            return []

        self._stats.total_attempts += 1
        self._arb_in_flight = True

        try:
            if opp.mode == ArbMode.MERGE:
                await self._execute_merge_arb(opp)
            else:
                await self._execute_split_arb(opp)
        except Exception as exc:
            logger.exception("[%s] 套利执行异常: %s", self.name, exc)
            self._stats.failures += 1
        finally:
            self._arb_in_flight = False
            self._last_arb_ts = time.monotonic()

        return []

    # ------------------------------------------------------------------ #
    # 机会检测
    # ------------------------------------------------------------------ #

    def _detect_opportunity(
        self,
        yes_data: MarketData,
        no_data: MarketData,
    ) -> Optional[ArbOpportunity]:
        """
        扫描订单簿，识别 Merge 或 Split 套利机会。

        Merge: 检查 YES_ask + NO_ask < 1 - min_merge_spread
        Split: 检查 YES_bid + NO_bid > 1 + min_split_spread
        """
        yes_ask = yes_data.best_ask
        no_ask  = no_data.best_ask
        yes_bid = yes_data.best_bid
        no_bid  = no_data.best_bid

        # ---- Merge 套利 -------------------------------------------
        if yes_ask is not None and no_ask is not None:
            ask_sum = yes_ask + no_ask
            gross_per_unit = 1.0 - ask_sum   # 每 1 USDC 面值的毛利润

            # 流动性检查：双边 ask 层的可用 shares 必须满足最低要求
            yes_ask_depth = self._get_ask_depth(yes_data, yes_ask)
            no_ask_depth  = self._get_ask_depth(no_data, no_ask)

            if gross_per_unit > self._cfg.min_merge_spread:
                if (yes_ask_depth >= self._cfg.liquidity_min_size and
                        no_ask_depth >= self._cfg.liquidity_min_size):

                    # 规模受双边深度限制（shares 转换为 USDC）
                    max_by_yes = yes_ask_depth * yes_ask
                    max_by_no  = no_ask_depth  * no_ask
                    trade_usdc = min(
                        self._cfg.base_trade_usdc,
                        self._cfg.max_trade_usdc,
                        max_by_yes,
                        max_by_no,
                    )

                    gross_profit = trade_usdc * gross_per_unit / ask_sum
                    net_profit   = gross_profit - self._cfg.estimated_gas_usdc

                    if net_profit > 0:
                        return ArbOpportunity(
                            mode=ArbMode.MERGE,
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
                        "[%s] Merge 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        self.name, ask_sum,
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
                if (yes_bid_depth >= self._cfg.liquidity_min_size and
                        no_bid_depth >= self._cfg.liquidity_min_size):

                    max_by_yes = yes_bid_depth * yes_bid
                    max_by_no  = no_bid_depth  * no_bid
                    trade_usdc = min(
                        self._cfg.base_trade_usdc,
                        self._cfg.max_trade_usdc,
                        max_by_yes,
                        max_by_no,
                    )

                    gross_profit = trade_usdc * gross_per_unit
                    net_profit   = gross_profit - self._cfg.estimated_gas_usdc

                    if net_profit > 0:
                        return ArbOpportunity(
                            mode=ArbMode.SPLIT,
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
                        "[%s] Split 价差满足 (sum=%.4f) 但流动性不足 "
                        "(%s_depth=%.1f  %s_depth=%.1f  需=%.1f)",
                        self.name, bid_sum,
                        yes_data.outcome, yes_bid_depth,
                        no_data.outcome,  no_bid_depth,
                        self._cfg.liquidity_min_size,
                    )

        logger.debug(
            "[%s] 无套利机会 — %s ask=%.4f bid=%.4f | %s ask=%.4f bid=%.4f | "
            "merge_sum=%.4f  split_sum=%.4f",
            self.name,
            yes_data.outcome, yes_ask or 0, yes_bid or 0,
            no_data.outcome,  no_ask  or 0, no_bid  or 0,
            (yes_ask or 0) + (no_ask or 0),
            (yes_bid or 0) + (no_bid or 0),
        )
        return None

    # ------------------------------------------------------------------ #
    # Merge 套利执行
    # ------------------------------------------------------------------ #

    async def _execute_merge_arb(self, opp: ArbOpportunity) -> None:
        """
        步骤：
          1. 并发 FOK 买入 YES 和 NO（毫秒级同时下单）
          2. 检查双边是否全部成交
          3. 若双边成交 → merge_positions → 得到 pUSD
          4. 若有一边未成交 → 记录警告（头寸不平衡，需人工处理或单独止损）
        """
        t0 = time.perf_counter()

        # 买入数量（shares）= USDC / 2 / price（双边各花一半）
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
            strategy_tag=f"{self.name}_merge_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="BUY",
            size=half_usdc,
            price=round(min(0.99, opp.no_price + self._cfg.slippage_tolerance), 4),
            order_type="FOK",
            strategy_tag=f"{self.name}_merge_no",
        )

        logger.info(
            "[%s] MERGE — 并发买入 %s %.4f shares @%.4f + %s %.4f shares @%.4f  "
            "总规模=%.2f USDC  预期净利=$%.4f",
            self.name,
            opp.yes_outcome, yes_shares, yes_req.price,
            opp.no_outcome,  no_shares,  no_req.price,
            opp.trade_size_usdc, opp.net_profit,
        )

        # 并发提交双边买单（核心：两笔 FOK 同时发出，最小化价格漂移风险）
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )

        buy_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[%s] MERGE 买单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            self.name,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            buy_ms,
        )

        if yes_result.success and no_result.success:
            # 双边均成交，执行链上 merge
            merge_amount = min(yes_shares, no_shares)
            await self._do_merge(opp.condition_id, merge_amount, opp.net_profit)
        elif yes_result.success and not no_result.success:
            logger.error(
                "[%s] MERGE 风险！%s 成交但 %s 未成交 — 需要人工平仓 %s 头寸",
                self.name, opp.yes_outcome, opp.no_outcome, opp.yes_outcome,
            )
            self._stats.failures += 1
        elif not yes_result.success and no_result.success:
            logger.error(
                "[%s] MERGE 风险！%s 成交但 %s 未成交 — 需要人工平仓 %s 头寸",
                self.name, opp.no_outcome, opp.yes_outcome, opp.no_outcome,
            )
            self._stats.failures += 1
        else:
            logger.warning("[%s] MERGE 双边均未成交，无头寸风险。", self.name)
            self._stats.failures += 1

    async def _do_merge(
        self,
        condition_id: str,
        amount: float,
        expected_net_profit: float,
    ) -> None:
        """调用 SDK merge_positions，将 YES+NO 合并为 pUSD。"""
        t0 = time.perf_counter()
        try:
            result = await self._client.merge_positions(
                condition_id=condition_id,
                amount=amount,
            )
            merge_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[%s] ✅ MERGE 完成 — tx=%s  耗时=%.0fms  预期净利=$%.4f",
                self.name, result.get("transaction_hash", ""), merge_ms, expected_net_profit,
            )
            self._stats.successes += 1
            self._stats.total_net_profit += expected_net_profit
        except Exception as exc:
            logger.exception("[%s] merge_positions 失败: %s", self.name, exc)
            self._stats.failures += 1
            raise

    # ------------------------------------------------------------------ #
    # Split 套利执行
    # ------------------------------------------------------------------ #

    async def _execute_split_arb(self, opp: ArbOpportunity) -> None:
        """
        步骤：
          1. split_position — 将 pUSD 拆成等量 YES + NO
          2. 并发 FOK 卖出 YES 和 NO
          3. 记录利润
        """
        t0 = time.perf_counter()

        # split_amount = USDC 面值（拆分后得到 split_amount 个 YES 和 split_amount 个 NO）
        split_amount = opp.trade_size_usdc

        logger.info(
            "[%s] SPLIT — 拆分 %.2f pUSD  %s_bid=%.4f  %s_bid=%.4f  "
            "预期净利=$%.4f",
            self.name,
            split_amount,
            opp.yes_outcome, opp.yes_price,
            opp.no_outcome,  opp.no_price,
            opp.net_profit,
        )

        # Step 1: 链上 split
        try:
            split_result = await self._client.split_position(
                condition_id=opp.condition_id,
                amount=split_amount,
            )
            split_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[%s] SPLIT tx 完成 — tx=%s  耗时=%.0fms",
                self.name, split_result.get("transaction_hash", ""), split_ms,
            )
        except Exception as exc:
            logger.exception("[%s] split_position 失败: %s", self.name, exc)
            self._stats.failures += 1
            raise

        # Step 2: 并发 FOK 卖出双边（用拿到的 bid 减去滑点容忍）
        yes_sell_price = round(max(0.01, opp.yes_price - self._cfg.slippage_tolerance), 4)
        no_sell_price  = round(max(0.01, opp.no_price  - self._cfg.slippage_tolerance), 4)

        yes_req = OrderRequest(
            token_id=opp.yes_token_id,
            condition_id=opp.condition_id,
            outcome=opp.yes_outcome,
            side="SELL",
            size=split_amount * opp.yes_price,  # USDC 价値
            price=yes_sell_price,
            order_type="FOK",
            strategy_tag=f"{self.name}_split_yes",
        )
        no_req = OrderRequest(
            token_id=opp.no_token_id,
            condition_id=opp.condition_id,
            outcome=opp.no_outcome,
            side="SELL",
            size=split_amount * opp.no_price,
            price=no_sell_price,
            order_type="FOK",
            strategy_tag=f"{self.name}_split_no",
        )

        t1 = time.perf_counter()
        yes_result, no_result = await asyncio.gather(
            self._order_manager.place_order(yes_req),
            self._order_manager.place_order(no_req),
        )
        sell_ms = (time.perf_counter() - t1) * 1000

        logger.info(
            "[%s] SPLIT 卖单完成 — %s ok=%s  %s ok=%s  耗时=%.1fms",
            self.name,
            opp.yes_outcome, yes_result.success,
            opp.no_outcome,  no_result.success,
            sell_ms,
        )

        if yes_result.success and no_result.success:
            total_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[%s] ✅ SPLIT 套利完成 — 总耗时=%.0fms  预期净利=$%.4f",
                self.name, total_ms, opp.net_profit,
            )
            self._stats.successes += 1
            self._stats.total_net_profit += opp.net_profit
        else:
            logger.error(
                "[%s] SPLIT 卖单未完全成交 — %s ok=%s  %s ok=%s  "
                "已拆分头寸可能需要人工处理",
                self.name,
                opp.yes_outcome, yes_result.success,
                opp.no_outcome,  no_result.success,
            )
            self._stats.failures += 1

    # ------------------------------------------------------------------ #
    # 订单簿深度辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_ask_depth(data: MarketData, price: float) -> float:
        """返回 best ask 价格层的可用 shares（用于流动性检查）。"""
        for level in data.asks:
            if abs(level.price - price) < 1e-6:
                return level.size
        # 若无精确匹配则返回 0（保守估计）
        return 0.0

    @staticmethod
    def _get_bid_depth(data: MarketData, price: float) -> float:
        """返回 best bid 价格层的可用 shares（用于流动性检查）。"""
        for level in data.bids:
            if abs(level.price - price) < 1e-6:
                return level.size
        return 0.0


# ------------------------------------------------------------------ #
# 统计记录
# ------------------------------------------------------------------ #

@dataclass
class _ArbStats:
    total_attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_net_profit: float = 0.0
