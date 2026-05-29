"""
BTC 5-minute up/down strategy — placeholder implementation.

This file is the primary customisation point.  Replace the logic inside
``on_tick`` with your actual signal generation.

Current behaviour
-----------------
The placeholder does nothing — it logs the market snapshot and returns no
orders.  This allows you to run the bot in dry-run mode to verify
connectivity and data flow before writing real strategy logic.

Customisation guide
-------------------
1. Implement signal logic in ``_generate_signal()``.
2. Convert the signal into one or more ``OrderRequest`` objects.
3. Use ``self._state`` to persist intra-session state across ticks.
4. Keep parameters in ``StrategyConfig`` so they can be tuned without
   touching control-flow code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config.settings import settings
from core.market_data import MarketData
from core.order_manager import OrderRequest, OrderResult
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Strategy-specific configuration
# ------------------------------------------------------------------ #

@dataclass
class StrategyConfig:
    """
    Tune these values to control strategy behaviour.

    All price thresholds are expressed in [0, 1] (probability, not cents).
    """

    # ---- Entry thresholds -------------------------------------------
    # Only enter a YES-long position when the YES mid-price is below this.
    yes_entry_max_price: float = 0.55

    # Only enter a YES-short (NO-long) position when YES mid-price is above this.
    yes_entry_min_price: float = 0.45

    # ---- Order sizing -----------------------------------------------
    # Fixed size in USDC notional per order.
    order_notional_usdc: float = 10.0

    # Offset from mid-price to place a passive limit order (avoid crossing spread).
    limit_offset: float = 0.01

    # ---- Position limits --------------------------------------------
    # Maximum number of simultaneously open orders on one side.
    max_open_orders_per_side: int = 1


# ------------------------------------------------------------------ #
# Signal enum
# ------------------------------------------------------------------ #

class Signal(str, Enum):
    NONE = "NONE"
    BUY_YES = "BUY_YES"   # Bet that BTC will be higher
    BUY_NO = "BUY_NO"    # Bet that BTC will be lower


# ------------------------------------------------------------------ #
# Strategy state  (persisted across ticks within a session)
# ------------------------------------------------------------------ #

@dataclass
class _StrategyState:
    open_yes_orders: int = 0
    open_no_orders: int = 0
    last_signal: Signal = Signal.NONE


# ------------------------------------------------------------------ #
# Strategy implementation
# ------------------------------------------------------------------ #

class Btc5MinStrategy(BaseStrategy):
    """
    Placeholder strategy for the BTC 5-minute up/down market.

    Replace ``_generate_signal`` with real logic.
    """

    name = "btc_5min"

    def __init__(self, config: Optional[StrategyConfig] = None) -> None:
        self._cfg = config or StrategyConfig()
        self._state = _StrategyState()

    # ------------------------------------------------------------------ #
    # BaseStrategy interface
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        logger.info(
            "[%s] Strategy started.  Config: %s", self.name, self._cfg
        )

    def on_tick(self, yes_data: MarketData, no_data: MarketData) -> list[OrderRequest]:
        if not yes_data.is_valid or not no_data.is_valid:
            logger.warning("[%s] Incomplete market data — skipping tick.", self.name)
            return []

        logger.info(
            "[%s] Tick — YES mid=%.4f  NO mid=%.4f  spread=%.4f",
            self.name,
            yes_data.midpoint or 0,
            no_data.midpoint or 0,
            yes_data.spread or 0,
        )

        signal = self._generate_signal(yes_data, no_data)

        if signal == Signal.NONE:
            logger.debug("[%s] No signal this tick.", self.name)
            return []

        return self._build_orders(signal, yes_data, no_data)

    def on_order_result(self, request: OrderRequest, result: OrderResult) -> None:
        if result.success:
            if request.outcome == "YES":
                self._state.open_yes_orders += 1
            else:
                self._state.open_no_orders += 1

    def on_stop(self) -> None:
        logger.info("[%s] Strategy stopping.", self.name)

    # ------------------------------------------------------------------ #
    # *** CUSTOMISE HERE ***
    # ------------------------------------------------------------------ #

    def _generate_signal(self, yes_data: MarketData, no_data: MarketData) -> Signal:
        """
        Core signal logic.

        Replace this method body with your own model / rule-set.

        Examples of what might go here
        --------------------------------
        * Technical indicators computed from `MarketData.bids/asks`
        * External price feed comparison (e.g. current BTC spot price vs
          Polymarket implied probability)
        * ML model inference
        * Mean-reversion on the spread

        Must return a ``Signal`` enum value and MUST NOT raise exceptions.
        """
        # ----------------------------------------------------------------
        # PLACEHOLDER — always returns NONE (bot does nothing)
        # ----------------------------------------------------------------
        return Signal.NONE

    # ------------------------------------------------------------------ #
    # Order construction
    # ------------------------------------------------------------------ #

    def _build_orders(
        self,
        signal: Signal,
        yes_data: MarketData,
        no_data: MarketData,
    ) -> list[OrderRequest]:
        requests: list[OrderRequest] = []

        if signal == Signal.BUY_YES:
            if self._state.open_yes_orders >= self._cfg.max_open_orders_per_side:
                logger.debug("[%s] YES order limit reached — skipping.", self.name)
                return []

            mid = yes_data.midpoint or 0.5
            limit_price = round(mid - self._cfg.limit_offset, 4)
            size = round(self._cfg.order_notional_usdc / limit_price, 2)

            requests.append(OrderRequest(
                token_id=yes_data.token_id,
                condition_id=yes_data.condition_id,
                outcome="YES",
                side="BUY",
                size=size,
                price=limit_price,
                strategy_tag=self.name,
            ))

        elif signal == Signal.BUY_NO:
            if self._state.open_no_orders >= self._cfg.max_open_orders_per_side:
                logger.debug("[%s] NO order limit reached — skipping.", self.name)
                return []

            mid = no_data.midpoint or 0.5
            limit_price = round(mid - self._cfg.limit_offset, 4)
            size = round(self._cfg.order_notional_usdc / limit_price, 2)

            requests.append(OrderRequest(
                token_id=no_data.token_id,
                condition_id=no_data.condition_id,
                outcome="NO",
                side="BUY",
                size=size,
                price=limit_price,
                strategy_tag=self.name,
            ))

        return requests
