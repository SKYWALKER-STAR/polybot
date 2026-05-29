"""
Thin, stateful wrapper around the Polymarket CLOB Python client.

Responsibilities
----------------
* Initialise and hold the authenticated ClobClient instance.
* Translate SDK types into plain dicts / dataclasses so the rest of the
  codebase does not depend on py-clob-client internals.
* Raise descriptive exceptions on transport or auth errors.

Authentication flow
-------------------
1. The bot calls ``connect()`` once at startup.
2. If L2 API credentials (api_key / api_secret / api_passphrase) are
   configured, they are used for order-writing endpoints.
3. If they are absent, the bot falls back to deriving them from the wallet
   private key via ``derive_api_key()``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

from config.settings import settings

logger = logging.getLogger(__name__)

# Map human-friendly strings to the constants used by py-clob-client.
BUY = "BUY"
SELL = "SELL"


class PolymarketClient:
    """Authenticated wrapper around ClobClient."""

    def __init__(self) -> None:
        self._client: Optional[ClobClient] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """
        Establish authenticated connection to the CLOB API.
        Must be called once before any other method.
        """
        creds: Optional[ApiCreds] = None
        if settings.api_key:
            creds = ApiCreds(
                api_key=settings.api_key,
                api_secret=settings.api_secret or "",
                api_passphrase=settings.api_passphrase or "",
            )

        self._client = ClobClient(
            host=settings.clob_host,
            chain_id=settings.chain_id,
            key=settings.private_key,
            creds=creds,
        )

        # If no creds were provided, derive them now so order-write endpoints work.
        if creds is None:
            logger.info("No API credentials found — deriving from private key …")
            resp = self._client.derive_api_key()
            derived = ApiCreds(
                api_key=resp.get("apiKey", ""),
                api_secret=resp.get("secret", ""),
                api_passphrase=resp.get("passphrase", ""),
            )
            self._client.set_api_creds(derived)
            logger.info("API credentials derived successfully.")

        logger.info("Connected to Polymarket CLOB at %s", settings.clob_host)

    @property
    def client(self) -> ClobClient:
        if self._client is None:
            raise RuntimeError("PolymarketClient not connected — call connect() first.")
        return self._client

    # ------------------------------------------------------------------ #
    # Market data  (read-only, no auth required)
    # ------------------------------------------------------------------ #

    def get_market(self, condition_id: str) -> dict[str, Any]:
        """Return metadata for a single market."""
        return self.client.get_market(condition_id)  # type: ignore[return-value]

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Return the full order book for a token (YES or NO side)."""
        return self.client.get_order_book(token_id)  # type: ignore[return-value]

    def get_last_trade_price(self, token_id: str) -> str:
        return self.client.get_last_trade_price(token_id)  # type: ignore[return-value]

    def get_midpoint(self, token_id: str) -> str:
        return self.client.get_midpoint(token_id)  # type: ignore[return-value]

    def get_spread(self, token_id: str) -> str:
        return self.client.get_spread(token_id)  # type: ignore[return-value]

    def get_tick_size(self, token_id: str) -> str:
        return self.client.get_tick_size(token_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    # Order management  (requires auth)
    # ------------------------------------------------------------------ #

    def create_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: OrderType = OrderType.GTC,
    ) -> dict[str, Any]:
        """
        Sign and submit a limit order.

        Parameters
        ----------
        token_id:   CLOB token ID (YES or NO token).
        side:       "BUY" or "SELL".
        size:       Number of shares (not USDC notional).
        price:      Limit price in [0, 1] (binary probability).
        order_type: GTC (default), FOK, or GTD.

        Returns
        -------
        Raw exchange response dict (includes ``orderID`` on success).
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, order_type)  # type: ignore[return-value]

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a single open order by its exchange order ID."""
        return self.client.cancel(order_id)  # type: ignore[return-value]

    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel every open order on the account."""
        return self.client.cancel_all()  # type: ignore[return-value]

    def get_open_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return open orders, optionally filtered by market or asset."""
        kwargs: dict[str, Any] = {}
        if market:
            kwargs["market"] = market
        if asset_id:
            kwargs["asset_id"] = asset_id
        return self.client.get_orders(**kwargs)  # type: ignore[return-value]

    def get_positions(self) -> list[dict[str, Any]]:
        """Return current token positions held by the account."""
        return self.client.get_positions()  # type: ignore[return-value]

    def get_balance(self) -> dict[str, Any]:
        """Return USDC balance and allowance information."""
        return self.client.get_balance_allowance()  # type: ignore[return-value]
