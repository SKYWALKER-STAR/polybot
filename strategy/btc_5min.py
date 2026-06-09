"""
BTC 5分钟涨跌策略

交易逻辑
--------
当距离本轮市场结算时间还剩 ``entry_seconds_before_settlement`` 秒（默认 60s）时：
  - 若 UP（涨）的 best_ask 落在 [target_price - tolerance, target_price + tolerance] 范围内，则：
      · 买入 ``main_bet_usdc`` 美元的 UP（主仓）
      · 买入 ``hedge_bet_usdc`` 美元的 DOWN（对冲仓，可设为 0 关闭）
  - 若 DOWN（跌）的 best_ask 落在该范围内，则：
      · 买入 ``main_bet_usdc`` 美元的 DOWN（主仓）
      · 买入 ``hedge_bet_usdc`` 美元的 UP（对冲仓，可设为 0 关闭）
  - 每轮结算周期只入场一次，防止重复下单。

修改押注金额
-----------
只需修改 ``StrategyConfig`` 中的两个字段即可：
  - ``main_bet_usdc``  — 主方向押注金额
  - ``hedge_bet_usdc`` — 对冲方向押注金额
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from core.market_data import MarketData
from core.order_manager import OrderRequest, OrderResult
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# ★ 策略参数配置 — 修改押注金额请在此处调整 ★
# ------------------------------------------------------------------ #

@dataclass
class StrategyConfig:
    """
    策略运行参数。

    金额均为 USDC，概率阈值范围 0~1（如 0.90 代表 90%）。
    """

    # ---- 资金配置（主要修改入口）-------------------------------------
    # FOK 市价单押注金额（USDC）—— 立即吃单，保证即时全部成交。设为 0 可关闭
    fok_bet_usdc: float = 1.0

    # FAK 市价单押注金额（USDC）—— 尽量吃单，剩余部分自动取消。设为 0 可关闭
    fak_bet_usdc: float = 0.0

    # GTC 限价单押注金额（USDC）—— 挂单等待更优价格成交。设为 0 可关闭
    gtc_bet_usdc: float = 1.0

    # 对冲方向押注金额（USDC）
    hedge_bet_usdc: float = 0.0

    # ---- 入场触发条件 -----------------------------------------------
    # 距离结算还剩多少秒内开始检查并入场
    entry_seconds_before_settlement: int = 60

    # 目标入场价格（0~1），例如 0.80 = 80%
    # 当某方向的 best_ask 落在 [target_price - tolerance, target_price + tolerance] 时触发
    target_price: float = 0.80

    # 价格容忍带（0~1），例如 0.03 = ±3%
    price_tolerance: float = 0.03

    # ---- 限价单价格偏移 ---------------------------------------------
    # 实际下单价格 = best_ask - limit_price_offset
    # 0.0  → 直接以 best_ask 挂单（贴近市价）
    # 0.01 → 比当前最优卖价低 1 分，更省钱但可能不成交
    # 负值 → 比 best_ask 更高，确保优先成交（不建议）
    limit_price_offset: float = 0.0


# ------------------------------------------------------------------ #
# Signal
# ------------------------------------------------------------------ #

class Signal(str, Enum):
    NONE    = "NONE"
    BUY_UP   = "BUY_UP"    # UP 概率   >= 阈值：主押 UP，对冲 DOWN
    BUY_DOWN = "BUY_DOWN"  # DOWN 概率 >= 阈值：主押 DOWN，对冲 UP


# ------------------------------------------------------------------ #
# 策略内部状态（会话级别持久化）
# ------------------------------------------------------------------ #

@dataclass
class _StrategyState:
    # 上次下单时的市场 condition_id（用于避免同一周期重复入场）
    bet_condition_id: Optional[str] = None
    
    # 待重试的订单
    pending_retries: list[OrderRequest] = field(default_factory=list)


# ------------------------------------------------------------------ #
# 策略实现
# ------------------------------------------------------------------ #

class Btc5MinStrategy(BaseStrategy):
    """BTC 5分钟涨跌策略主类。"""

    name = "btc_5min"

    def __init__(self, config: Optional[StrategyConfig] = None) -> None:
        self._cfg = config or StrategyConfig()
        self._state = _StrategyState()

    # ------------------------------------------------------------------ #
    # BaseStrategy 接口
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        logger.info(
            "[%s] 策略启动 — FOK %.2f USDC + FAK %.2f USDC + GTC %.2f USDC，对冲 %.2f USDC，"
            "目标价格 %.2f ±%.0f%%，入场窗口 %ds",
            self.name,
            self._cfg.fok_bet_usdc,
            self._cfg.fak_bet_usdc,
            self._cfg.gtc_bet_usdc,
            self._cfg.hedge_bet_usdc,
            self._cfg.target_price,
            self._cfg.price_tolerance * 100,
            self._cfg.entry_seconds_before_settlement,
        )

    def on_tick(self, up_data: MarketData, down_data: MarketData) -> list[OrderRequest]:
        if not up_data.is_valid or not down_data.is_valid:
            logger.warning("[%s] 行情数据不完整，跳过本次 tick。", self.name)
            return []

        signal = self._generate_signal(up_data, down_data)
        
        # 优先处理重试订单
        orders_to_submit = self._state.pending_retries
        self._state.pending_retries = []

        if signal != Signal.NONE:
            new_orders = self._build_orders(signal, up_data, down_data)
            if new_orders:
                # 标记本轮结算周期已入场，避免重复下单
                self._state.bet_condition_id = up_data.condition_id
                orders_to_submit.extend(new_orders)

        return orders_to_submit

    def on_order_result(self, request: OrderRequest, result: OrderResult) -> None:
        if result.success:
            logger.info(
                "[%s] 订单提交成功 — outcome=%s side=%s price=%.4f size=%.2f "
                "local_id=%s exchange_id=%s dry_run=%s",
                self.name,
                request.outcome,
                request.side,
                request.price,
                request.size,
                result.local_order_id,
                result.exchange_order_id,
                result.is_dry_run,
            )
        else:
            logger.warning(
                "[%s] 订单提交失败，将在下个 tick 重试 — outcome=%s error=%s",
                self.name, request.outcome, result.error,
            )
            self._state.pending_retries.append(request)

    def on_stop(self) -> None:
        logger.info("[%s] 策略停止。", self.name)

    # ------------------------------------------------------------------ #
    # 信号生成
    # ------------------------------------------------------------------ #

    def _generate_signal(self, up_data: MarketData, down_data: MarketData) -> Signal:
        """
        核心信号逻辑：
          1. 检查是否已获取结算时间。
          2. 检查距离结算是否在入场窗口内（<= entry_seconds_before_settlement）。
          3. 检查是否已在本周期入场过。
          4. 判断 UP 或 DOWN 的最优卖价是否达到阈值。
        """
        end_time = up_data.market_end_time
        if end_time is None:
            logger.warning("[%s] 未能获取市场结算时间，跳过。", self.name)
            return Signal.NONE

        now = datetime.now(timezone.utc)
        seconds_remaining = (end_time - now).total_seconds()

        # 结算已过，等待新一轮市场开启
        if seconds_remaining <= 0:
            return Signal.NONE

        # 未进入入场窗口
        if seconds_remaining > self._cfg.entry_seconds_before_settlement:
            logger.debug(
                "[%s] 距结算 %.0f 秒，尚未进入 %d 秒入场窗口。",
                self.name, seconds_remaining, self._cfg.entry_seconds_before_settlement,
            )
            return Signal.NONE

        # 本周期已经入场过（以 condition_id 为去重键，字符串比较更可靠）
        if self._state.bet_condition_id != up_data.condition_id:
            # 新周期开始，清空上一周期的重试订单
            if self._state.pending_retries:
                logger.info("[%s] 新周期开始，清空 %d 个上一周期的重试订单。",
                            self.name, len(self._state.pending_retries))
                self._state.pending_retries = []
        elif self._state.bet_condition_id == up_data.condition_id:
            logger.debug("[%s] 本周期已入场，不重复下单。", self.name)
            return Signal.NONE

        # 价格使用 best_ask（与 Polymarket UI 显示一致）
        # UI 上 "Up 22¢" 即 UP best_ask=0.22，两侧之和 > 1.0 属正常（包含 spread）
        up_price   = up_data.best_ask
        down_price = down_data.best_ask

        if up_price is None or down_price is None:
            logger.warning("[%s] best_ask 数据缺失，跳过。", self.name)
            return Signal.NONE

        # 流动性检查：spread 过大说明订单簿空虚，价格不可信，跳过
        max_spread = 0.5
        up_spread   = up_data.spread   if up_data.spread   is not None else 1.0
        down_spread = down_data.spread if down_data.spread is not None else 1.0
        if up_spread > max_spread or down_spread > max_spread:
            logger.info(
                "[%s] 流动性不足，跳过（up_spread=%.3f  down_spread=%.3f  阈值=%.2f）",
                self.name, up_spread, down_spread, max_spread,
            )
            return Signal.NONE

        lo = self._cfg.target_price - self._cfg.price_tolerance
        hi = self._cfg.target_price + self._cfg.price_tolerance

        logger.info(
            "[%s] ★ 入场窗口 ★ 距结算 %.0fs  UP=%.4f  DOWN=%.4f  目标区间=[%.2f, %.2f]",
            self.name, seconds_remaining, up_price, down_price, lo, hi,
        )

        if lo <= up_price <= hi:
            logger.info("[%s] 信号: UP 价格 %.4f 在 [%.2f, %.2f] → 主押 UP",
                        self.name, up_price, lo, hi)
            return Signal.BUY_UP

        if lo <= down_price <= hi:
            logger.info("[%s] 信号: DOWN 价格 %.4f 在 [%.2f, %.2f] → 主押 DOWN",
                        self.name, down_price, lo, hi)
            return Signal.BUY_DOWN

        logger.info(
            "[%s] 两侧价格均不在目标区间（UP=%.4f, DOWN=%.4f, 区间=[%.2f, %.2f]），不入场。",
            self.name, up_price, down_price, lo, hi,
        )
        return Signal.NONE

    # ------------------------------------------------------------------ #
    # 构建订单（主仓 + 对冲仓）
    # ------------------------------------------------------------------ #

    def _build_orders(
        self,
        signal: Signal,
        up_data: MarketData,
        down_data: MarketData,
    ) -> list[OrderRequest]:
        """
        根据信号生成两笔订单：
          - 主仓：押注概率高的一侧，花费 main_bet_usdc USDC
          - 对冲仓：押注反方向，花费 hedge_bet_usdc USDC

        订单类型使用 FOK（Fill-Or-Kill）：临近结算时要求立即成交，
        若无法立即全部成交则自动取消，避免挂单残留到结算后。
        """
        if signal == Signal.BUY_UP:
            main_token, main_outcome, main_data = up_data.token_id,   "UP",   up_data
            hedge_token, hedge_outcome, hedge_data = down_data.token_id, "DOWN", down_data
        else:  # BUY_DOWN
            main_token, main_outcome, main_data = down_data.token_id, "DOWN", down_data
            hedge_token, hedge_outcome, hedge_data = up_data.token_id,   "UP",   up_data

        # 限价单价格 = best_ask - offset，限制在 [0.01, 0.99]
        raw_main_ask  = main_data.best_ask  or 0.5
        raw_hedge_ask = hedge_data.best_ask or 0.5
        fok_price  = round(max(0.01, min(0.99, raw_main_ask)), 4)
        gtc_price  = round(max(0.01, min(0.99, raw_main_ask - self._cfg.limit_price_offset)), 4)
        hedge_price = round(max(0.01, min(0.99, raw_hedge_ask - self._cfg.limit_price_offset)), 4)

        orders: list[OrderRequest] = []

        if self._cfg.fok_bet_usdc > 0:
            orders.append(OrderRequest(
                token_id=main_token,
                condition_id=main_data.condition_id,
                outcome=main_outcome,
                side="BUY",
                size=self._cfg.fok_bet_usdc,
                price=fok_price,
                order_type="FOK",
                strategy_tag=self.name,
            ))

        if self._cfg.fak_bet_usdc > 0:
            orders.append(OrderRequest(
                token_id=main_token,
                condition_id=main_data.condition_id,
                outcome=main_outcome,
                side="BUY",
                size=self._cfg.fak_bet_usdc,
                price=fok_price,
                order_type="FAK",
                strategy_tag=f"{self.name}_fak",
            ))

        if self._cfg.gtc_bet_usdc > 0:
            orders.append(OrderRequest(
                token_id=main_token,
                condition_id=main_data.condition_id,
                outcome=main_outcome,
                side="BUY",
                size=self._cfg.gtc_bet_usdc,
                price=gtc_price,
                order_type="GTC",
                strategy_tag=f"{self.name}_gtc",
            ))

        if self._cfg.hedge_bet_usdc > 0:
            orders.append(OrderRequest(
                token_id=hedge_token,
                condition_id=hedge_data.condition_id,
                outcome=hedge_outcome,
                side="BUY",
                size=self._cfg.hedge_bet_usdc,
                price=hedge_price,
                order_type="GTC",
                strategy_tag=f"{self.name}_hedge",
            ))

        logger.info(
            "[%s] 构建订单 — 主仓 %s: FOK %.2f USDC @%.4f + FAK %.2f USDC @%.4f + GTC %.2f USDC @%.4f%s",
            self.name,
            main_outcome,
            self._cfg.fok_bet_usdc, fok_price,
            self._cfg.fak_bet_usdc, fok_price,
            self._cfg.gtc_bet_usdc, gtc_price,
            f"  对冲: {hedge_outcome} GTC @{hedge_price:.4f} (≈${self._cfg.hedge_bet_usdc:.2f})"
            if self._cfg.hedge_bet_usdc > 0 else "  对冲: 已关闭",
        )

        return orders
