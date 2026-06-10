"""
Position Tracker
================
Tracks open positions built up from successfully placed BUY orders and
computes real-time P&L using WebSocket best_bid prices.

Design
------
* A ``Position`` is created/updated every time a BUY order is successfully
  submitted (``record_fill``).  Entry price is the order limit price (a good
  proxy for the actual fill price; improves once User-Channel fills land).
* P&L is computed as:
      pnl_pct = (current_bid - avg_entry_price) / avg_entry_price * 100
  where ``current_bid`` comes from the in-memory WebSocket feed — this is
  the fastest available price (sub-second latency).
* On stop-loss, ``bot.py`` calls ``get_liquidation_order()`` which returns
  an ``OrderRequest`` sized to sell all accumulated shares at the current
  best_bid.
* After the liquidation order is placed, call ``close_position()`` to clear
  the internal state so the stop-loss does not fire again.

Thread / Async safety
---------------------
All mutations happen from the single asyncio event loop thread in ``bot.py``.
No locking is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.order_manager import OrderRequest

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """State for a single open position on one token."""

    token_id: str
    outcome: str          # "UP" or "DOWN"
    condition_id: str
    market_slug: str

    # ---- accounting (weighted-average) --------------------------------
    # Total shares we hold (sum of size_usdc / entry_price for each fill)
    shares: float = 0.0
    # Total USDC spent buying these shares
    cost_usdc: float = 0.0
    # Weighted-average entry price (cost_usdc / shares)
    avg_entry_price: float = 0.0

    # Timestamp of the first fill recorded for this position
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # True once a liquidation order has been placed (prevents double-firing)
    liquidating: bool = False

    def add_fill(self, price: float, size_usdc: float) -> None:
        """
        Record a new fill into this position.

        ``price``     — limit price at which we placed the order (proxy fill price).
        ``size_usdc`` — USDC amount spent on this fill.
        """
        new_shares = size_usdc / price if price > 0 else 0.0
        self.shares    += new_shares
        self.cost_usdc += size_usdc
        if self.shares > 0:
            self.avg_entry_price = self.cost_usdc / self.shares

    def pnl_pct(self, current_bid: float) -> float:
        """
        Return the unrealised P&L as a percentage of cost.

        Positive  = profit
        Negative  = loss

        Formula: (current_bid - avg_entry) / avg_entry * 100
        """
        if self.avg_entry_price <= 0:
            return 0.0
        return (current_bid - self.avg_entry_price) / self.avg_entry_price * 100.0

    def liquidation_order(
        self,
        current_bid: float,
        order_type: str = "GTC",
        strategy_tag: str = "stop_loss",
    ) -> OrderRequest:
        """
        Build an ``OrderRequest`` that sells all shares at ``current_bid``.

        order_type:
          - GTC : 挂限价卖单，价格不成交会留在订单簿上持续等待
          - FOK : 必须立即全部成交，否则整单取消
          - FAK : 尽量立即成交，剩余部分自动取消
        """
        size_usdc = round(self.shares * current_bid, 6)
        price = round(max(0.01, min(0.99, current_bid)), 4)

        return OrderRequest(
            token_id=self.token_id,
            condition_id=self.condition_id,
            outcome=self.outcome,
            side="SELL",
            size=size_usdc,
            price=price,
            order_type=order_type,
            strategy_tag=strategy_tag,
            market_slug=self.market_slug,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Position {self.outcome} shares={self.shares:.4f} "
            f"avg_entry={self.avg_entry_price:.4f} cost={self.cost_usdc:.2f} USDC>"
        )


class PositionTracker:
    """
    Maintains a dictionary of open ``Position`` objects keyed by ``token_id``.

    Usage in ``bot.py``::

        tracker = PositionTracker()

        # After a successful BUY order:
        tracker.record_fill(
            token_id=req.token_id,
            outcome=req.outcome,
            condition_id=req.condition_id,
            market_slug=req.market_slug,
            price=req.price,
            size_usdc=req.size,
        )

        # In each tick, check for stop-loss:
        for pos in tracker.get_positions_to_liquidate(
            up_bid=up_data.best_bid,
            down_bid=down_data.best_bid,
            stop_loss_pct=settings.strategy_stop_loss_pct,
            up_token_id=up_data.token_id,
            down_token_id=down_data.token_id,
        ):
            order = pos.liquidation_order(current_bid)
            tracker.mark_liquidating(pos.token_id)
            await order_manager.place_order(order)
    """

    def __init__(self) -> None:
        # token_id → Position
        self._positions: dict[str, Position] = {}

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #

    def record_fill(
        self,
        token_id: str,
        outcome: str,
        condition_id: str,
        market_slug: str,
        price: float,
        size_usdc: float,
    ) -> None:
        """
        Record a buy fill.  Creates a new ``Position`` if one doesn't exist.
        """
        if price <= 0 or size_usdc <= 0:
            return

        pos = self._positions.get(token_id)
        if pos is None:
            pos = Position(
                token_id=token_id,
                outcome=outcome,
                condition_id=condition_id,
                market_slug=market_slug,
            )
            self._positions[token_id] = pos

        pos.add_fill(price, size_usdc)
        logger.info(
            "[PositionTracker] 持仓更新 — %s %s  新增 %.2f USDC @%.4f  "
            "累计持股=%.4f  均价=%.4f  总成本=%.2f USDC",
            outcome, token_id[:8],
            size_usdc, price,
            pos.shares, pos.avg_entry_price, pos.cost_usdc,
        )

    def mark_liquidating(self, token_id: str) -> None:
        """Mark a position as 'liquidation in progress' to prevent double-firing."""
        pos = self._positions.get(token_id)
        if pos:
            pos.liquidating = True

    def close_position(self, token_id: str) -> None:
        """Remove position after liquidation is confirmed or market settles."""
        if token_id in self._positions:
            pos = self._positions.pop(token_id)
            logger.info(
                "[PositionTracker] 持仓关闭 — %s %s  总成本=%.2f USDC",
                pos.outcome, token_id[:8], pos.cost_usdc,
            )

    def clear_all(self) -> None:
        """Clear all tracked positions (e.g. after market settlement)."""
        count = len(self._positions)
        self._positions.clear()
        if count:
            logger.info("[PositionTracker] 清空所有持仓（共 %d 笔）", count)

    def sync_from_exchange(
        self,
        condition_id: str,
        outcome_to_token_id: dict[str, str],
        positions: list[dict],
    ) -> None:
        """
        用交易所官方持仓覆盖当前市场本地仓位（source of truth）。

        仅同步传入 ``condition_id`` 对应市场，避免误删其他市场状态。
        """
        live_token_ids: set[str] = set()

        for item in positions:
            market_id = str(
                item.get("market_id")
                or item.get("condition_id")
                or item.get("market")
                or ""
            )
            if market_id != condition_id:
                continue

            outcome = _normalize_exchange_outcome(str(item.get("outcome") or ""))
            token_id = outcome_to_token_id.get(outcome)
            if not token_id:
                continue

            shares = _safe_float(item.get("size"))
            if shares <= 0:
                continue

            value = _safe_float(item.get("current_value"))
            existing = self._positions.get(token_id)
            liquidating = existing.liquidating if existing is not None else False
            opened_at = existing.opened_at if existing is not None else datetime.now(timezone.utc)
            market_slug = existing.market_slug if existing is not None else ""

            # 若本次 value 缺失，保留原成本，避免均价被错误归零。
            cost_usdc = value if value > 0 else (existing.cost_usdc if existing is not None else 0.0)
            avg_entry_price = cost_usdc / shares if cost_usdc > 0 and shares > 0 else 0.0

            self._positions[token_id] = Position(
                token_id=token_id,
                outcome=outcome,
                condition_id=condition_id,
                market_slug=market_slug,
                shares=shares,
                cost_usdc=cost_usdc,
                avg_entry_price=avg_entry_price,
                opened_at=opened_at,
                liquidating=liquidating,
            )
            live_token_ids.add(token_id)

        stale_token_ids = [
            token_id
            for token_id, pos in self._positions.items()
            if pos.condition_id == condition_id and token_id not in live_token_ids
        ]
        for token_id in stale_token_ids:
            self._positions.pop(token_id, None)

        logger.debug(
            "[PositionTracker] 官方持仓同步完成 — condition=%s count=%d",
            condition_id,
            len(live_token_ids),
        )

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #

    def get_position(self, token_id: str) -> Optional[Position]:
        return self._positions.get(token_id)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_positions_to_liquidate(
        self,
        token_bid_map: dict[str, Optional[float]],
        stop_loss_pct: float,
    ) -> list[tuple[Position, float]]:
        """
        Return a list of ``(position, current_bid)`` for every position whose
        loss has reached ``stop_loss_pct`` (expressed as a positive percentage,
        e.g. 20.0 means liquidate when position is down ≥20%).

        Only positions that are not already in the ``liquidating`` state are
        returned.

        Parameters
        ----------
        token_bid_map : dict[token_id → best_bid]
            Current best_bid for each tracked token, from WsMarketFeed.
        stop_loss_pct : float
            Positive loss threshold.  E.g. 20.0 triggers when P&L ≤ -20%.
        """
        to_liquidate: list[tuple[Position, float]] = []

        for token_id, pos in self._positions.items():
            if pos.liquidating:
                continue

            current_bid = token_bid_map.get(token_id)
            if current_bid is None or current_bid <= 0:
                logger.debug(
                    "[PositionTracker] %s 无 best_bid，跳过止损检查", token_id[:8]
                )
                continue

            pnl = pos.pnl_pct(current_bid)
            logger.debug(
                "[PositionTracker] %s %s  P&L=%.2f%%  bid=%.4f  avg_entry=%.4f",
                pos.outcome, token_id[:8], pnl, current_bid, pos.avg_entry_price,
            )

            if pnl <= -abs(stop_loss_pct):
                logger.warning(
                    "[PositionTracker] ⚠ 止损触发！%s %s  P&L=%.2f%%  "
                    "阈值=%.1f%%  bid=%.4f  均价=%.4f  总成本=%.2f USDC",
                    pos.outcome, token_id[:8], pnl,
                    stop_loss_pct, current_bid, pos.avg_entry_price, pos.cost_usdc,
                )
                to_liquidate.append((pos, current_bid))

        return to_liquidate

    def get_positions_to_take_profit(
        self,
        token_bid_map: dict[str, Optional[float]],
        take_profit_pct: float,
    ) -> list[tuple[Position, float]]:
        """
        Return a list of ``(position, current_bid)`` for every position whose
        profit has reached ``take_profit_pct`` (expressed as a positive percentage,
        e.g. 20.0 means close when position is up ≥20%).

        Only positions that are not already in the ``liquidating`` state are
        returned.

        Parameters
        ----------
        token_bid_map : dict[token_id → best_bid]
            Current best_bid for each tracked token.
        take_profit_pct : float
            Positive profit threshold.  E.g. 20.0 triggers when P&L ≥ +20%.
        """
        to_close: list[tuple[Position, float]] = []

        for token_id, pos in self._positions.items():
            if pos.liquidating:
                continue

            current_bid = token_bid_map.get(token_id)
            if current_bid is None or current_bid <= 0:
                logger.debug(
                    "[PositionTracker] %s 无 best_bid，跳过止盈检查", token_id[:8]
                )
                continue

            pnl = pos.pnl_pct(current_bid)
            logger.debug(
                "[PositionTracker] %s %s  P&L=%.2f%%  bid=%.4f  avg_entry=%.4f",
                pos.outcome, token_id[:8], pnl, current_bid, pos.avg_entry_price,
            )

            if pnl >= abs(take_profit_pct):
                logger.info(
                    "[PositionTracker] ✓ 止盈触发！%s %s  P&L=%.2f%%  "
                    "阈值=%.1f%%  bid=%.4f  均价=%.4f  总成本=%.2f USDC",
                    pos.outcome, token_id[:8], pnl,
                    take_profit_pct, current_bid, pos.avg_entry_price, pos.cost_usdc,
                )
                to_close.append((pos, current_bid))

        return to_close

    def __len__(self) -> int:
        return len(self._positions)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PositionTracker positions={len(self._positions)}>"


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_exchange_outcome(value: str) -> str:
    normalized = value.strip().upper()
    mapping = {
        "UP": "UP",
        "DOWN": "DOWN",
        "YES": "UP",
        "NO": "DOWN",
    }
    return mapping.get(normalized, normalized)
