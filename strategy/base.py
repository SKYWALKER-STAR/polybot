"""
Abstract base class for all trading strategies.

A strategy executes one tick and returns a (possibly empty) list of
OrderRequest objects. The strategy itself can decide whether it pulls market
data internally or uses injected dependencies.
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

from core.order_manager import OrderRequest, OrderResult

logger = logging.getLogger(__name__)

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
    async def on_tick(self) -> list[OrderRequest]:
        """
      Called once per poll cycle.

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

    def bind(self, **kwargs: Any) -> None:
        """
        注入基础设施依赖（client、order_manager、ws_feed 等）。

        在 bot.start() 创建好基础设施后、调用 on_start() 前被调用。
        各子类按需 override 并声明自己需要的关键字参数；
        多余的 kwargs 统一用 **kwargs 忽略，保证接口向前兼容。
        """

    async def on_start(self) -> None:
        """Called once when the bot starts, before the first tick."""

    def on_stop(self) -> None:
        """Called once when the bot is shutting down gracefully."""
