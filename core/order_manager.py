"""
Order manager — the single point through which all orders flow.

Responsibilities
----------------
* Accept order requests from the strategy layer.
* Enforce risk limits (max order size, dry-run mode).
* Submit orders to the exchange via PolymarketClient.
* Persist every order and its status transitions to the database.
* Delegate audit logging to AuditLogger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from py_clob_client.clob_types import OrderType

from config.settings import settings
from core.client import PolymarketClient
from database.connection import get_session
from database.models import (
    AuditAction,
    AuditResult,
    Order,
    OrderSide,
    OrderStatus,
    Outcome,
    OrderType as DbOrderType,
)

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """Value object produced by a strategy and consumed by OrderManager."""

    token_id: str
    condition_id: str
    outcome: str               # "YES" or "NO"
    side: str                  # "BUY" or "SELL"
    size: float                # number of shares
    price: float               # limit price in [0, 1]
    order_type: str = "GTC"    # GTC | FOK | GTD
    strategy_tag: str = ""
    market_slug: str = ""


@dataclass
class OrderResult:
    """Outcome returned to the strategy after an order attempt."""

    success: bool
    local_order_id: Optional[int]      # PK in the orders table
    exchange_order_id: Optional[str]   # Polymarket order ID
    is_dry_run: bool
    error: Optional[str] = None


class OrderManager:
    """
    Central order gateway — all order operations go through this class.

    Parameters
    ----------
    client:       Authenticated PolymarketClient.
    audit_logger: AuditLogger instance for recording actions.
    """

    def __init__(self, client: PolymarketClient, audit_logger: "AuditLogger") -> None:  # noqa: F821
        self._client = client
        self._audit = audit_logger

    # ------------------------------------------------------------------ #
    # Place order
    # ------------------------------------------------------------------ #

    def place_order(self, req: OrderRequest) -> OrderResult:
        """
        Validate, risk-check, and submit a limit order.

        In dry-run mode the order is recorded locally but never sent to the
        exchange.
        """
        # --- risk checks ------------------------------------------------
        notional = req.size * req.price
        if notional > settings.max_order_size_usdc:
            msg = (
                f"Order rejected: notional {notional:.2f} USDC exceeds "
                f"max_order_size_usdc={settings.max_order_size_usdc}"
            )
            logger.warning(msg)
            self._audit.record(
                action=AuditAction.PLACE_ORDER,
                result=AuditResult.FAILURE,
                details={"request": _req_to_dict(req)},
                error_message=msg,
            )
            return OrderResult(success=False, local_order_id=None,
                               exchange_order_id=None, is_dry_run=settings.dry_run,
                               error=msg)

        # --- persist locally (status = PENDING) -------------------------
        local_order = self._create_local_order(req)

        # --- dry-run shortcut -------------------------------------------
        if settings.dry_run:
            logger.info(
                "[DRY-RUN] Would place %s %s order — outcome=%s price=%.4f size=%.2f",
                req.side, req.order_type, req.outcome, req.price, req.size,
            )
            self._update_order_status(local_order.id, OrderStatus.OPEN, dry_run=True)
            self._audit.record(
                action=AuditAction.PLACE_ORDER,
                result=AuditResult.SKIPPED,
                details={"local_order_id": local_order.id, "dry_run": True,
                         "request": _req_to_dict(req)},
            )
            return OrderResult(success=True, local_order_id=local_order.id,
                               exchange_order_id=None, is_dry_run=True)

        # --- live submission --------------------------------------------
        try:
            sdk_order_type = _map_order_type(req.order_type)
            resp = self._client.create_limit_order(
                token_id=req.token_id,
                side=req.side,
                size=req.size,
                price=req.price,
                order_type=sdk_order_type,
            )

            exchange_order_id: Optional[str] = resp.get("orderID") or resp.get("order_id")

            self._update_order_status(
                local_order.id,
                OrderStatus.OPEN,
                exchange_order_id=exchange_order_id,
            )

            logger.info(
                "Order placed — local_id=%s exchange_id=%s outcome=%s side=%s "
                "price=%.4f size=%.2f",
                local_order.id, exchange_order_id, req.outcome, req.side,
                req.price, req.size,
            )
            self._audit.record(
                action=AuditAction.PLACE_ORDER,
                result=AuditResult.SUCCESS,
                details={
                    "local_order_id": local_order.id,
                    "exchange_order_id": exchange_order_id,
                    "request": _req_to_dict(req),
                    "response": resp,
                },
            )
            return OrderResult(
                success=True,
                local_order_id=local_order.id,
                exchange_order_id=exchange_order_id,
                is_dry_run=False,
            )

        except Exception as exc:
            logger.exception("Failed to place order: %s", exc)
            self._update_order_status(local_order.id, OrderStatus.FAILED)
            self._audit.record(
                action=AuditAction.PLACE_ORDER,
                result=AuditResult.FAILURE,
                details={"local_order_id": local_order.id, "request": _req_to_dict(req)},
                error_message=str(exc),
            )
            return OrderResult(
                success=False,
                local_order_id=local_order.id,
                exchange_order_id=None,
                is_dry_run=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Cancel order
    # ------------------------------------------------------------------ #

    def cancel_order(self, local_order_id: int) -> bool:
        """
        Cancel an order by its local database ID.

        Returns True on success.
        """
        order = self._load_order(local_order_id)
        if order is None:
            logger.error("Cannot cancel: order id=%s not found", local_order_id)
            return False

        if settings.dry_run:
            logger.info("[DRY-RUN] Would cancel order id=%s", local_order_id)
            self._update_order_status(local_order_id, OrderStatus.CANCELLED)
            self._audit.record(
                action=AuditAction.CANCEL_ORDER,
                result=AuditResult.SKIPPED,
                details={"local_order_id": local_order_id, "dry_run": True},
            )
            return True

        if not order.polymarket_order_id:
            logger.warning(
                "Order id=%s has no exchange ID — cannot cancel remotely", local_order_id
            )
            return False

        try:
            self._client.cancel_order(order.polymarket_order_id)
            self._update_order_status(local_order_id, OrderStatus.CANCELLED)
            logger.info(
                "Cancelled order local_id=%s exchange_id=%s",
                local_order_id, order.polymarket_order_id,
            )
            self._audit.record(
                action=AuditAction.CANCEL_ORDER,
                result=AuditResult.SUCCESS,
                details={
                    "local_order_id": local_order_id,
                    "exchange_order_id": order.polymarket_order_id,
                },
            )
            return True

        except Exception as exc:
            logger.exception("Failed to cancel order id=%s: %s", local_order_id, exc)
            self._audit.record(
                action=AuditAction.CANCEL_ORDER,
                result=AuditResult.FAILURE,
                details={"local_order_id": local_order_id},
                error_message=str(exc),
            )
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders on the exchange account."""
        if settings.dry_run:
            logger.info("[DRY-RUN] Would cancel all open orders")
            self._audit.record(
                action=AuditAction.CANCEL_ALL,
                result=AuditResult.SKIPPED,
                details={"dry_run": True},
            )
            return True

        try:
            self._client.cancel_all_orders()
            logger.info("All open orders cancelled.")
            self._audit.record(
                action=AuditAction.CANCEL_ALL,
                result=AuditResult.SUCCESS,
            )
            return True
        except Exception as exc:
            logger.exception("Failed to cancel all orders: %s", exc)
            self._audit.record(
                action=AuditAction.CANCEL_ALL,
                result=AuditResult.FAILURE,
                error_message=str(exc),
            )
            return False

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _create_local_order(self, req: OrderRequest) -> Order:
        order = Order(
            condition_id=req.condition_id,
            token_id=req.token_id,
            market_slug=req.market_slug or None,
            side=OrderSide(req.side),
            outcome=Outcome(req.outcome),
            order_type=DbOrderType(req.order_type),
            price=req.price,
            size=req.size,
            size_matched=0.0,
            status=OrderStatus.PENDING,
            is_dry_run=settings.dry_run,
            strategy_tag=req.strategy_tag or None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        with get_session() as session:
            session.add(order)
            session.flush()
            session.refresh(order)
            return order

    def _update_order_status(
        self,
        local_id: int,
        status: OrderStatus,
        exchange_order_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        with get_session() as session:
            order = session.get(Order, local_id)
            if order:
                order.status = status
                order.updated_at = datetime.now(timezone.utc)
                if exchange_order_id:
                    order.polymarket_order_id = exchange_order_id
                if dry_run:
                    order.is_dry_run = True

    def _load_order(self, local_id: int) -> Optional[Order]:
        with get_session() as session:
            return session.get(Order, local_id)


# ------------------------------------------------------------------ #
# Utilities
# ------------------------------------------------------------------ #

def _req_to_dict(req: OrderRequest) -> dict:
    return {
        "token_id": req.token_id,
        "condition_id": req.condition_id,
        "outcome": req.outcome,
        "side": req.side,
        "size": req.size,
        "price": req.price,
        "order_type": req.order_type,
        "strategy_tag": req.strategy_tag,
    }


def _map_order_type(order_type_str: str) -> OrderType:
    mapping = {
        "GTC": OrderType.GTC,
        "FOK": OrderType.FOK,
        "GTD": OrderType.GTD,
    }
    return mapping.get(order_type_str.upper(), OrderType.GTC)


# Avoid circular import — import AuditLogger type for annotation only.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from audit.logger import AuditLogger
