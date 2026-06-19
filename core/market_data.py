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
from core.market_resolver import MarketInfo, MarketResolver
from core.ws_market_feed import WsMarketFeed
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
    outcome: str                    # "UP" or "DOWN"

    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    midpoint: Optional[float]
    last_trade_price: Optional[float]

    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    # 市场结算时间（ET），由 MarketResolver 从 Event 信息中获取
    market_end_time: Optional[datetime] = None
    # Gamma API outcomePrices 提供的市场概率价格（UI 上显示的价格）
    gamma_price: Optional[float] = None

    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """True when we have at least a mid-price to work with."""
        return self.midpoint is not None or self.last_trade_price is not None


class MarketDataService:
    """
    Fetches market data for the BTC 5-minute market and persists snapshots.

    通过 MarketResolver 动态跟踪当前活跃的 5-min 市场，市场到期后自动切换到下一个。

    Example
    -------
    ::

        svc = MarketDataService(client, resolver=MarketResolver(1780073700))
        up_data, down_data = await svc.fetch()
    """

    def __init__(
        self,
        client: PolymarketClient,
        resolver: MarketResolver,
        persist_snapshots: bool = True,
        ws_feed: Optional[WsMarketFeed] = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self.persist_snapshots = persist_snapshots
        self._ws_feed = ws_feed
        # 以下三个字段由 _sync_market() 在每次 fetch 前更新
        self.condition_id: str = ""
        self.up_token_id: str = ""
        self.down_token_id: str = ""
        self._current_market: Optional[MarketInfo] = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def _sync_market(self) -> MarketInfo:
        """
        从 MarketResolver 获取当前有效市场，并同步 condition_id / token_id。
        若市场发生切换，打印日志并动态订阅新 token。
        """
        info = await self._resolver.get_active_market()
        is_new_market = (
            self._current_market is None
            or self._current_market.condition_id != info.condition_id
        )
        if is_new_market:
            logger.info(
                "MarketDataService — 切换市场: slug=%s  condition=%s  "
                "结算时间(UTC)=%s  剩余=%.0fs",
                info.slug,
                info.condition_id,
                info.end_time.isoformat(),
                info.seconds_to_expiry,
            )
            # 订阅新市场的 token，等待 WS 推送订单射快照
            if self._ws_feed is not None:
                await self._ws_feed.subscribe_tokens(
                    info.up_token_id, info.down_token_id
                )
        self._current_market = info
        self.condition_id = info.condition_id
        self.up_token_id = info.up_token_id
        self.down_token_id = info.down_token_id
        return info

    async def fetch(self) -> tuple[MarketData, MarketData]:
        """
        Fetch the current order book for both UP and DOWN tokens.

        If a ``WsMarketFeed`` is attached and has received data, use its
        in-memory cache instead of making HTTP requests.

        Returns (up_data, down_data).
        """
        market_info = await self._sync_market()
        market_end_time = market_info.end_time

        up_data = await self.fetch_token_book(
            self.up_token_id,
            outcome="UP",
            condition_id=market_info.condition_id,
            market_end_time=market_end_time,
            gamma_price=market_info.up_price,
        )
        down_data = await self.fetch_token_book(
            self.down_token_id,
            outcome="DOWN",
            condition_id=market_info.condition_id,
            market_end_time=market_end_time,
            gamma_price=market_info.down_price,
        )

        if self.persist_snapshots:
            self._save_snapshots(up_data, down_data)

        logger.debug(
            "Market snapshot — UP mid=%.4f  DOWN mid=%.4f  end_time=%s",
            up_data.midpoint or 0,
            down_data.midpoint or 0,
            market_end_time,
        )
        return up_data, down_data

    async def fetch_up(self) -> MarketData:
        info = await self._sync_market()
        return await self.fetch_token_book(
            self.up_token_id,
            outcome="UP",
            condition_id=info.condition_id,
            market_end_time=info.end_time,
            gamma_price=info.up_price,
        )

    async def fetch_down(self) -> MarketData:
        info = await self._sync_market()
        return await self.fetch_token_book(
            self.down_token_id,
            outcome="DOWN",
            condition_id=info.condition_id,
            market_end_time=info.end_time,
            gamma_price=info.down_price,
        )

    async def fetch_token_book(
        self,
        token_id: str,
        outcome: str,
        *,
        condition_id: str = "",
        market_end_time: Optional[datetime] = None,
        gamma_price: Optional[float] = None,
        prefer_ws: bool = True,
    ) -> MarketData:
        if prefer_ws and self._ws_feed is not None:
            await self._ws_feed.subscribe_tokens(token_id)
            if self._ws_feed.is_ready():
                data = self._build_from_ws(
                    token_id=token_id,
                    outcome=outcome,
                    condition_id=condition_id,
                    market_end_time=market_end_time,
                )
                if data.is_valid or data.bids or data.asks:
                    data.gamma_price = gamma_price
                    return data

        data = await self._fetch_token(
            token_id=token_id,
            outcome=outcome,
            condition_id=condition_id,
            market_end_time=market_end_time,
        )
        data.gamma_price = gamma_price
        return data

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_from_ws(
        self,
        token_id: str,
        outcome: str,
        condition_id: str = "",
        market_end_time: Optional[datetime] = None,
    ) -> MarketData:
        """Build a MarketData snapshot from the WsMarketFeed in-memory state."""
        assert self._ws_feed is not None
        state = self._ws_feed.get_snapshot(token_id)

        best_bid: Optional[float] = None
        best_ask: Optional[float] = None
        last_trade_price: Optional[float] = None
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []

        if state is not None:
            best_bid         = state.best_bid
            best_ask         = state.best_ask
            last_trade_price = state.last_trade_price
            bids = [
                OrderBookLevel(price=float(price), size=size)
                for price, size in sorted(
                    state.bid_levels.items(),
                    key=lambda item: float(item[0]),
                )
                if size > 0
            ]
            asks = [
                OrderBookLevel(price=float(price), size=size)
                for price, size in sorted(
                    state.ask_levels.items(),
                    key=lambda item: float(item[0]),
                    reverse=True,
                )
                if size > 0
            ]

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

        return MarketData(
            condition_id=condition_id or self.condition_id,
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
        )

    async def _fetch_token(
        self,
        token_id: str,
        outcome: str,
        condition_id: str = "",
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
        
        best_bid = bids[-1].price if bids else None   # bids 升序，最高买价在末尾
        best_ask = asks[-1].price if asks else None   # asks 降序，最低卖价在末尾

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
            condition_id=condition_id or self.condition_id,
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
