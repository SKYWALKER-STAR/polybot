"""
Market data service.

Fetches and normalises order-book / price information from Polymarket,
and persists snapshots to the database for historical analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.client import PolymarketClient
from database.connection import get_session
from database.models import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class MarketData:
    """Normalised snapshot of one token's order book."""

    condition_id: str
    token_id: str
    outcome: str                    # "YES" or "NO"

    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    midpoint: Optional[float]
    last_trade_price: Optional[float]

    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    # 市场结算时间（UTC），由 MarketDataService 从 get_market() 获取
    market_end_time: Optional[datetime] = None

    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """True when we have at least a mid-price to work with."""
        return self.midpoint is not None or self.last_trade_price is not None


class MarketDataService:
    """
    Fetches market data for the BTC 5-minute market and persists snapshots.

    Example
    -------
    ::

        svc = MarketDataService(client, condition_id, yes_token_id, no_token_id)
        yes_data, no_data = svc.fetch()
    """

    def __init__(
        self,
        client: PolymarketClient,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        persist_snapshots: bool = True,
    ) -> None:
        self._client = client
        self.condition_id = condition_id
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.persist_snapshots = persist_snapshots

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch(self) -> tuple[MarketData, MarketData]:
        """
        Fetch the current order book for both YES and NO tokens.

        Returns (yes_data, no_data).
        """
        market_end_time = await self._fetch_market_end_time()

        yes_data = await self._fetch_token(self.yes_token_id, "YES", market_end_time)
        no_data = await self._fetch_token(self.no_token_id, "NO", market_end_time)

        if self.persist_snapshots:
            self._save_snapshots(yes_data, no_data)

        logger.debug(
            "Market snapshot — YES mid=%.4f  NO mid=%.4f  end_time=%s",
            yes_data.midpoint or 0,
            no_data.midpoint or 0,
            market_end_time,
        )
        return yes_data, no_data

    async def fetch_yes(self) -> MarketData:
        return await self._fetch_token(self.yes_token_id, "YES", await self._fetch_market_end_time())

    async def fetch_no(self) -> MarketData:
        return await self._fetch_token(self.no_token_id, "NO", await self._fetch_market_end_time())

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _fetch_market_end_time(self) -> Optional[datetime]:
        """从 SDK 市场模型中提取结算时间（UTC）。"""
        try:
            market = await self._client.get_market(self.condition_id)
            # New SDK: market.state.end_date is a datetime | None
            state = getattr(market, "state", None)
            end_date = getattr(state, "end_date", None)
            if end_date is not None:
                if end_date.tzinfo is None:
                    return end_date.replace(tzinfo=timezone.utc)
                return end_date
        except Exception as exc:
            logger.warning("获取市场结算时间失败: %s", exc)
        return None

    async def _fetch_token(
        self,
        token_id: str,
        outcome: str,
        market_end_time: Optional[datetime] = None,
    ) -> MarketData:
        # Single request — all price fields are derived from the order book.
        book = await self._client.get_order_book(token_id)

        # The SDK returns model objects; bids/asks are sequences of entries
        # with .price and .size attributes (Decimal).
        raw_bids = getattr(book, "bids", None) or []
        raw_asks = getattr(book, "asks", None) or []

        bids = [
            OrderBookLevel(price=float(lvl.price), size=float(lvl.size))
            for lvl in raw_bids
        ]
        asks = [
            OrderBookLevel(price=float(lvl.price), size=float(lvl.size))
            for lvl in raw_asks
        ]

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None

        midpoint: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2
        elif best_bid is not None:
            midpoint = best_bid
        elif best_ask is not None:
            midpoint = best_ask

        spread: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        last_trade_price = _to_float(getattr(book, "last_trade_price", None))

        return MarketData(
            condition_id=self.condition_id,
            token_id=token_id,
            outcome=outcome,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            midpoint=midpoint,
            last_trade_price=last_trade_price,
            bids=bids,
            asks=asks,
            market_end_time=market_end_time,
            raw={},
        )

    def _save_snapshots(self, *snapshots: MarketData) -> None:
        try:
            with get_session() as session:
                for snap in snapshots:
                    session.add(
                        MarketSnapshot(
                            condition_id=snap.condition_id,
                            token_id=snap.token_id,
                            best_bid=snap.best_bid,
                            best_ask=snap.best_ask,
                            spread=snap.spread,
                            last_trade_price=snap.last_trade_price,
                            midpoint=snap.midpoint,
                            raw_order_book=snap.raw,
                            captured_at=snap.captured_at,
                        )
                    )
        except Exception as exc:
            # Snapshot persistence failure must never crash the main loop.
            logger.warning("Failed to persist market snapshot: %s", exc)


def _to_float(value: Any) -> Optional[float]:
    """Safely coerce an API response value to float."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
