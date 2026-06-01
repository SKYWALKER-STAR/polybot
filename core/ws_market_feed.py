"""
WebSocket Market Feed
=====================
Subscribes to the Polymarket CLOB WebSocket market channel and maintains
an in-memory cache of the latest ``MarketData`` for each subscribed token.

Endpoint:  wss://ws-subscriptions-clob.polymarket.com/ws/market
Protocol:
  - Send subscription JSON immediately after connecting.
  - Send plain text ``PING`` every 10 s; server responds ``PONG``.
  - Receive JSON messages with ``event_type`` field.

Message types used:
  book            — full orderbook snapshot (on connect + after trades)
  best_bid_ask    — lightweight best-price update (custom_feature_enabled)
  last_trade_price — last matched trade price
  price_change    — individual price level changes (kept for spread calc)

Usage
-----
::

    feed = WsMarketFeed()
    asyncio.create_task(feed.run(up_token_id, down_token_id))
    # ... later, in your strategy tick:
    up_data, down_data = feed.get_data(up_token_id, down_token_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_PING_INTERVAL = 10          # seconds between PING heartbeats
_RECONNECT_DELAY_BASE = 2    # seconds, doubles on each failure up to max
_RECONNECT_DELAY_MAX  = 60


@dataclass
class _TokenState:
    """Live state for one token."""
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_trade_price: Optional[float] = None
    # Sparse level-2 book from price_change events
    # { price_str: size_float }  (size 0 = level removed)
    bid_levels: dict[str, float] = field(default_factory=dict)
    ask_levels: dict[str, float] = field(default_factory=dict)
    updated_at: Optional[datetime] = None

    def mark_updated(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class WsMarketFeed:
    """
    Long-running WebSocket subscription that keeps ``_TokenState`` up to date.

    Call ``run()`` as a background task.  Read state with ``get_snapshot()``.
    """

    def __init__(self) -> None:
        self._states: dict[str, _TokenState] = {}
        self._token_ids: list[str] = []
        self._ready = asyncio.Event()   # set once the first book snapshot is received
        self._stop  = asyncio.Event()
        self._ws: Any = None            # current live websocket connection

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Signal the background task to exit cleanly."""
        self._stop.set()

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until at least one book snapshot has been received."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def get_snapshot(self, token_id: str) -> Optional[_TokenState]:
        """Return current state for a token, or None if not yet received."""
        return self._states.get(token_id)

    def get_best_bid_ask(
        self, token_id: str
    ) -> tuple[Optional[float], Optional[float]]:
        """Return (best_bid, best_ask) for a token."""
        state = self._states.get(token_id)
        if state is None:
            return None, None
        return state.best_bid, state.best_ask

    async def subscribe_tokens(self, *token_ids: str) -> None:
        """
        Dynamically subscribe to additional token IDs without reconnecting.
        New ``_TokenState`` entries are created immediately so ``is_ready()``
        will temporarily return False for these tokens until a book snapshot
        arrives.
        """
        new_ids = [tid for tid in token_ids if tid not in self._states]
        if not new_ids:
            return

        for tid in new_ids:
            self._states[tid] = _TokenState(token_id=tid)
            self._token_ids.append(tid)

        # Clear ready flag — caller should wait_ready() again
        self._ready.clear()

        if self._ws is not None:
            try:
                sub_msg = json.dumps({
                    "assets_ids": new_ids,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
                await self._ws.send(sub_msg)
                logger.info(
                    "[WsFeed] Dynamically subscribed to %d new token(s): %s",
                    len(new_ids), [t[:8] for t in new_ids],
                )
            except Exception as exc:
                logger.warning("[WsFeed] Failed to send dynamic subscribe: %s", exc)
        else:
            logger.info(
                "[WsFeed] Queued %d token(s) for next connection.", len(new_ids)
            )

    # ------------------------------------------------------------------ #
    # Background task
    # ------------------------------------------------------------------ #

    async def run(self, *token_ids: str) -> None:
        """
        Connect, subscribe, and process messages until ``stop()`` is called.
        Reconnects automatically on any failure.
        """
        self._token_ids = list(token_ids)
        for tid in token_ids:
            if tid not in self._states:
                self._states[tid] = _TokenState(token_id=tid)

        delay = _RECONNECT_DELAY_BASE
        while not self._stop.is_set():
            try:
                await self._connect_and_run()
                delay = _RECONNECT_DELAY_BASE  # reset on clean exit
            except asyncio.CancelledError:
                logger.info("[WsFeed] Task cancelled — shutting down.")
                break
            except Exception as exc:
                logger.warning(
                    "[WsFeed] Connection error: %s — reconnecting in %ds", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)

    async def _connect_and_run(self) -> None:
        logger.info("[WsFeed] Connecting to %s …", _WS_URL)
        async with websockets.connect(_WS_URL, ping_interval=None) as ws:
            self._ws = ws
            logger.info("[WsFeed] Connected.")

            # Subscribe immediately (server may close idle connections)
            sub_msg = json.dumps({
                "assets_ids": self._token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            })
            await ws.send(sub_msg)
            logger.info("[WsFeed] Subscribed to %d token(s).", len(self._token_ids))

            try:
                # Run heartbeat + message receiver concurrently
                await asyncio.gather(
                    self._heartbeat(ws),
                    self._receive_loop(ws),
                )
            finally:
                self._ws = None

    async def _heartbeat(self, ws: Any) -> None:
        """Send PING every 10 s to keep the connection alive."""
        while not self._stop.is_set():
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await ws.send("PING")
                logger.debug("[WsFeed] → PING")
            except ConnectionClosed:
                break

    async def _receive_loop(self, ws: Any) -> None:
        """Process incoming messages until connection drops or stop is requested."""
        async for raw in ws:
            if self._stop.is_set():
                break

            # Server responds to our PING with plain text "PONG"
            if isinstance(raw, str) and raw.strip().upper() == "PONG":
                logger.debug("[WsFeed] ← PONG")
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("[WsFeed] Non-JSON message: %r", raw)
                continue

            # Server may batch multiple events into a JSON array
            if isinstance(msg, list):
                for item in msg:
                    if isinstance(item, dict):
                        self._dispatch(item)
            elif isinstance(msg, dict):
                self._dispatch(msg)
            else:
                logger.debug("[WsFeed] Unexpected message shape: %r", msg)

    # ------------------------------------------------------------------ #
    # Message dispatchers
    # ------------------------------------------------------------------ #

    def _dispatch(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type", "")
        if event_type == "book":
            self._on_book(msg)
        elif event_type == "best_bid_ask":
            self._on_best_bid_ask(msg)
        elif event_type == "last_trade_price":
            self._on_last_trade_price(msg)
        elif event_type == "price_change":
            self._on_price_change(msg)
        else:
            logger.debug("[WsFeed] Unknown event_type=%r", event_type)

    def _on_book(self, msg: dict[str, Any]) -> None:
        """
        Full orderbook snapshot.
        bids/asks format: [{"price": "0.48", "size": "30"}, ...]
        bids are ascending (best bid = last), asks are descending (best ask = last).
        """
        asset_id = msg.get("asset_id", "")
        state = self._states.get(asset_id)
        if state is None:
            return

        raw_bids: list[dict] = msg.get("bids", [])
        raw_asks: list[dict] = msg.get("asks", [])

        state.bid_levels = {
            lvl["price"]: float(lvl["size"]) for lvl in raw_bids
        }
        state.ask_levels = {
            lvl["price"]: float(lvl["size"]) for lvl in raw_asks
        }

        # best bid = highest bid price, best ask = lowest ask price
        if raw_bids:
            state.best_bid = float(raw_bids[-1]["price"])   # ascending → last = max
        if raw_asks:
            state.best_ask = float(raw_asks[-1]["price"])   # descending → last = min

        state.mark_updated()
        logger.debug(
            "[WsFeed] book %s  bid=%.4f  ask=%.4f",
            asset_id[:8], state.best_bid or 0, state.best_ask or 0,
        )

        # Mark feed as ready after first book snapshot
        if not self._ready.is_set():
            self._ready.set()
            logger.info("[WsFeed] Initial orderbook received — feed is ready.")

    def _on_best_bid_ask(self, msg: dict[str, Any]) -> None:
        """
        Lightweight best-price update (fastest path).
        Fields: asset_id, best_bid, best_ask, spread
        """
        asset_id = msg.get("asset_id", "")
        state = self._states.get(asset_id)
        if state is None:
            return

        bid = msg.get("best_bid")
        ask = msg.get("best_ask")
        if bid is not None:
            state.best_bid = _to_float(bid)
        if ask is not None:
            state.best_ask = _to_float(ask)
        state.mark_updated()

        logger.debug(
            "[WsFeed] best_bid_ask %s  bid=%.4f  ask=%.4f",
            asset_id[:8], state.best_bid or 0, state.best_ask or 0,
        )

    def _on_last_trade_price(self, msg: dict[str, Any]) -> None:
        asset_id = msg.get("asset_id", "")
        state = self._states.get(asset_id)
        if state is None:
            return

        state.last_trade_price = _to_float(msg.get("price"))
        state.mark_updated()
        logger.debug("[WsFeed] last_trade_price %s  price=%.4f", asset_id[:8], state.last_trade_price or 0)

    def _on_price_change(self, msg: dict[str, Any]) -> None:
        """
        Incremental price level update. Update sparse level-2 book and
        recalculate best bid/ask.
        """
        for change in msg.get("price_changes", []):
            asset_id = change.get("asset_id", "")
            state = self._states.get(asset_id)
            if state is None:
                continue

            # Use the best_bid/best_ask embedded in price_change when available
            best_bid = _to_float(change.get("best_bid"))
            best_ask = _to_float(change.get("best_ask"))
            if best_bid is not None and best_bid > 0:
                state.best_bid = best_bid
            if best_ask is not None and best_ask < 1:
                state.best_ask = best_ask

            price_str = change.get("price", "")
            size = _to_float(change.get("size", "0")) or 0.0
            side = change.get("side", "").upper()

            if side == "BUY":
                if size == 0:
                    state.bid_levels.pop(price_str, None)
                else:
                    state.bid_levels[price_str] = size
            elif side == "SELL":
                if size == 0:
                    state.ask_levels.pop(price_str, None)
                else:
                    state.ask_levels[price_str] = size

            state.mark_updated()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
