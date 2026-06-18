from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.market_data import MarketData, MarketDataService, OrderBookLevel


@dataclass(frozen=True)
class OrderBookTarget:
    """Reusable selector for any token-backed target market."""

    token_id: str
    outcome: str = "TARGET"
    condition_id: str = ""
    market_slug: str = ""
    market_end_time: Optional[datetime] = None
    gamma_price: Optional[float] = None
    prefer_ws: bool = True


@dataclass(frozen=True)
class OrderBookDepth:
    shares: float
    notional: float


@dataclass(frozen=True)
class OrderBookSlippage:
    requested_notional: float
    filled_notional: float
    filled_shares: float
    average_price: Optional[float]
    slippage: Optional[float]
    is_complete: bool


@dataclass(frozen=True)
class OrderBookMetrics:
    bid_ask_spread: Optional[float]
    bid_depth: OrderBookDepth
    ask_depth: OrderBookDepth
    depth_ratio: Optional[float]
    buy_slippage: OrderBookSlippage
    sell_slippage: OrderBookSlippage


@dataclass(frozen=True)
class OrderBookAnalysisResult:
    target: OrderBookTarget
    market_data: MarketData
    metrics: OrderBookMetrics


class OrderBookAnalyzer:
    """Reusable order-book analytics for any MarketData snapshot."""

    @classmethod
    def analyze(
        cls,
        market_data: MarketData,
        *,
        slippage_notional: float,
    ) -> OrderBookMetrics:
        bid_depth = cls._depth(market_data.bids)
        ask_depth = cls._depth(market_data.asks)

        depth_ratio: Optional[float] = None
        if ask_depth.notional > 0:
            depth_ratio = bid_depth.notional / ask_depth.notional

        return OrderBookMetrics(
            bid_ask_spread=market_data.spread,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            depth_ratio=depth_ratio,
            buy_slippage=cls._estimate_buy_slippage(
                market_data.asks,
                best_price=market_data.best_ask,
                requested_notional=slippage_notional,
            ),
            sell_slippage=cls._estimate_sell_slippage(
                market_data.bids,
                best_price=market_data.best_bid,
                requested_notional=slippage_notional,
            ),
        )

    @staticmethod
    def _depth(levels: list[OrderBookLevel]) -> OrderBookDepth:
        shares = sum(level.size for level in levels)
        notional = sum(level.price * level.size for level in levels)
        return OrderBookDepth(shares=shares, notional=notional)

    @classmethod
    def _estimate_buy_slippage(
        cls,
        asks: list[OrderBookLevel],
        *,
        best_price: Optional[float],
        requested_notional: float,
    ) -> OrderBookSlippage:
        return cls._estimate_slippage(
            levels=reversed(asks),
            best_price=best_price,
            requested_notional=requested_notional,
            is_buy=True,
        )

    @classmethod
    def _estimate_sell_slippage(
        cls,
        bids: list[OrderBookLevel],
        *,
        best_price: Optional[float],
        requested_notional: float,
    ) -> OrderBookSlippage:
        return cls._estimate_slippage(
            levels=reversed(bids),
            best_price=best_price,
            requested_notional=requested_notional,
            is_buy=False,
        )

    @staticmethod
    def _estimate_slippage(
        *,
        levels,
        best_price: Optional[float],
        requested_notional: float,
        is_buy: bool,
    ) -> OrderBookSlippage:
        if requested_notional <= 0:
            return OrderBookSlippage(
                requested_notional=requested_notional,
                filled_notional=0.0,
                filled_shares=0.0,
                average_price=None,
                slippage=None,
                is_complete=True,
            )

        remaining_notional = requested_notional
        filled_notional = 0.0
        filled_shares = 0.0

        for level in levels:
            if level.price <= 0 or level.size <= 0:
                continue

            level_notional = level.price * level.size
            taken_notional = min(remaining_notional, level_notional)
            taken_shares = taken_notional / level.price

            filled_notional += taken_notional
            filled_shares += taken_shares
            remaining_notional -= taken_notional

            if remaining_notional <= 1e-9:
                remaining_notional = 0.0
                break

        average_price: Optional[float] = None
        slippage: Optional[float] = None
        if filled_shares > 0:
            average_price = filled_notional / filled_shares
            if best_price is not None:
                if is_buy:
                    slippage = average_price - best_price
                else:
                    slippage = best_price - average_price

        return OrderBookSlippage(
            requested_notional=requested_notional,
            filled_notional=filled_notional,
            filled_shares=filled_shares,
            average_price=average_price,
            slippage=slippage,
            is_complete=remaining_notional == 0.0,
        )


class OrderBookService:
    """Fetch and analyze order books for arbitrary token targets."""

    def __init__(self, market_data_service: MarketDataService) -> None:
        self._market_data_service = market_data_service

    async def analyze_token(
        self,
        token_id: str,
        *,
        slippage_notional: float,
        outcome: str = "TARGET",
        condition_id: str = "",
        market_slug: str = "",
        market_end_time: Optional[datetime] = None,
        gamma_price: Optional[float] = None,
        prefer_ws: bool = True,
    ) -> OrderBookAnalysisResult:
        return await self.analyze_target(
            OrderBookTarget(
                token_id=token_id,
                outcome=outcome,
                condition_id=condition_id,
                market_slug=market_slug,
                market_end_time=market_end_time,
                gamma_price=gamma_price,
                prefer_ws=prefer_ws,
            ),
            slippage_notional=slippage_notional,
        )

    async def analyze_target(
        self,
        target: OrderBookTarget,
        *,
        slippage_notional: float,
    ) -> OrderBookAnalysisResult:
        market_data = await self._market_data_service.fetch_token_book(
            target.token_id,
            outcome=target.outcome,
            condition_id=target.condition_id,
            market_end_time=target.market_end_time,
            gamma_price=target.gamma_price,
            prefer_ws=target.prefer_ws,
        )
        metrics = OrderBookAnalyzer.analyze(
            market_data,
            slippage_notional=slippage_notional,
        )
        return OrderBookAnalysisResult(
            target=target,
            market_data=market_data,
            metrics=metrics,
        )

    async def analyze_targets(
        self,
        targets: list[OrderBookTarget],
        *,
        slippage_notional: float,
    ) -> list[OrderBookAnalysisResult]:
        if not targets:
            return []

        return await asyncio.gather(
            *[
                self.analyze_target(
                    target,
                    slippage_notional=slippage_notional,
                )
                for target in targets
            ]
        )