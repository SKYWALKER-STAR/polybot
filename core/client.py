"""
Async wrapper around the official Polymarket Python SDK (polymarket-client).

Responsibilities
----------------
* Initialise and hold an authenticated AsyncSecureClient instance.
* Translate SDK models into plain dicts so the rest of the codebase
  does not depend on SDK internals.
* Raise descriptive exceptions on transport or auth errors.

Authentication
--------------
The SDK authenticates with ``private_key``.  Set ``wallet_address`` in
settings when the signing key differs from the Polymarket wallet (e.g.
a hardware-wallet proxy setup).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from polymarket import AsyncSecureClient, PolymarketError

from config.settings import settings

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"


class PolymarketClient:
    """Async wrapper around AsyncSecureClient from the official SDK."""

    def __init__(self) -> None:
        self._client: Optional[AsyncSecureClient] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """
        Initialise the authenticated SDK client.
        Must be called once before any other method.
        """
        self._client = await AsyncSecureClient.create(
            private_key=settings.private_key,
            wallet=settings.wallet_address or None,
        )
        logger.info("Connected to Polymarket via AsyncSecureClient")

    async def close(self) -> None:
        """Release underlying network transports."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> AsyncSecureClient:
        if self._client is None:
            raise RuntimeError("PolymarketClient not connected — call connect() first.")
        return self._client

    # ------------------------------------------------------------------ #
    # Market data  (read-only)
    # ------------------------------------------------------------------ #

    async def get_event(self, slug: str) -> Any:
        """Return the Event SDK model for the given event slug."""
        return await self.client.get_event(slug=slug)

    async def get_order_book(self, token_id: str) -> Any:
        """Return the order book SDK model for a token."""
        return await self.client.get_order_book(token_id=token_id)

    # ------------------------------------------------------------------ #
    # Order management  (requires auth)
    # ------------------------------------------------------------------ #

    async def create_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        """
        Sign and submit an order via the official SDK.

        - GTC: place_limit_order — 按 price/size 下限价单
        - FOK: place_market_order(order_type="FOK") — 以 size*price USDC 为花费额市价单，
               无法全部成交则全部取消
        """
        if order_type in ("FOK", "FAK"):
            # FOK/FAK 市价单：amount = 押注 USDC 金额
            # FOK: 必须全部成交，否则全部取消
            # FAK: 能成交多少成交多少，剩余取消（流动性不足时更适合）
            amount = str(round(size * price, 6))
            response = await self.client.place_market_order(
                token_id=token_id,
                side=side,
                amount=amount,
                order_type=order_type,
            )
        else:
            response = await self.client.place_limit_order(
                token_id=token_id,
                side=side,
                price=str(price),
                size=str(size),
            )
        return {
            "ok": response.ok,
            "order_id": response.order_id if response.ok else None,
            "code": getattr(response, "code", None),
            "message": getattr(response, "message", None),
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a single open order by its exchange order ID."""
        await self.client.cancel_order(order_id=order_id)
        return {"ok": True}

    async def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel all open orders by iterating list_open_orders."""
        cancelled: list[str] = []
        async for page in self.client.list_open_orders():
            for order in page.items:
                try:
                    await self.client.cancel_order(order_id=order.order_id)
                    cancelled.append(order.order_id)
                except PolymarketError as exc:
                    logger.warning("Failed to cancel order %s: %s", order.order_id, exc)
        return {"cancelled": cancelled}

    async def get_open_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return open orders as a list of plain dicts."""
        kwargs: dict[str, Any] = {}
        if market:
            kwargs["market"] = market
        results: list[dict[str, Any]] = []
        async for page in self.client.list_open_orders(**kwargs):
            for o in page.items:
                results.append({
                    "order_id": o.order_id,
                    "token_id": str(getattr(o, "token_id", "")),
                    "side": str(getattr(o, "side", "")),
                    "price": float(o.price) if getattr(o, "price", None) is not None else None,
                    "size": float(o.size) if getattr(o, "size", None) is not None else None,
                    "status": str(getattr(o, "status", "")),
                })
        return results

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return current token positions for the authenticated wallet."""
        results: list[dict[str, Any]] = []
        async for page in self.client.list_positions():
            for pos in page.items:
                results.append({
                    "market_id": str(getattr(pos, "market_id", "")),
                    "outcome": str(getattr(pos, "outcome", "")),
                    "size": float(pos.size) if getattr(pos, "size", None) is not None else None,
                    "value": float(pos.value) if getattr(pos, "value", None) is not None else None,
                })
        return results

    async def get_balance(self) -> dict[str, Any]:
        """Return portfolio value for the authenticated wallet."""
        value = await self.client.get_portfolio_value()
        return {"portfolio_value": str(value) if value is not None else None}
