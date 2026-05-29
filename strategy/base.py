"""
Abstract base class for all trading strategies.

A strategy receives a pair of MarketData objects (YES token, NO token) on
every tick and returns a (possibly empty) list of OrderRequest objects.
The bot runner passes those requests to OrderManager without any further
interpretation.

Implementing a new strategy
----------------------------
1. Subclass ``BaseStrategy``.
2. Implement ``on_tick()``.
3. Optionally override ``on_order_result()`` to react to fill events.
4. Register the class in ``bot.py``.

Contract
--------
* ``on_tick`` MUST be side-effect-free with respect to the exchange.
  All exchange interaction happens in OrderManager.
* Strategies SHOULD be stateless across ticks when possible, or manage
  their own state clearly (e.g., storing last signal in ``self``).
* Strategies MUST NOT raise exceptions — use ``logger.warning`` and
  return an empty list instead.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from core.market_data import MarketData
from core.order_manager import OrderRequest, OrderResult

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Interface every trading strategy must implement.

    Attributes
    ----------
    name : str
        Human-readable identifier used in logs and the ``strategy_tag``
        column of the ``orders`` table.
    """

    name: str = "base"

    # ------------------------------------------------------------------ #
    # Required
    # ------------------------------------------------------------------ #

    @abstractmethod
    def on_tick(
        self,
        yes_data: MarketData,
        no_data: MarketData,
    ) -> list[OrderRequest]:
        """
        Called once per poll cycle with the latest market data.

        Parameters
        ----------
        yes_data: Current snapshot for the YES outcome token.
        no_data:  Current snapshot for the NO outcome token.

        Returns
        -------
        List of OrderRequest objects to be submitted.  Return an empty list
        to skip this tick.
        """

    # ------------------------------------------------------------------ #
    # Optional hooks
    # ------------------------------------------------------------------ #

    def on_order_result(self, request: OrderRequest, result: OrderResult) -> None:
        """
        Called after OrderManager attempts to place an order.

        Override to implement position tracking, fill-based re-quoting, etc.
        The default implementation does nothing.
        """

    def on_start(self) -> None:
        """Called once when the bot starts, before the first tick."""

    def on_stop(self) -> None:
        """Called once when the bot is shutting down gracefully."""
