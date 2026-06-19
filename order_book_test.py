import asyncio

from core.client import PolymarketClient
from core.market_data import MarketDataService
from core.market_resolver import MarketResolver
from core.order_book import OrderBookService

async def main():
    client = PolymarketClient()
    await client.connect()

    resolver = MarketResolver(initial_timestamp=1780073700)
    market_data_service = MarketDataService(client=client, resolver=resolver)
    order_book_service = OrderBookService(market_data_service)

    market = await resolver.get_active_market()
    report = await order_book_service.analyze_token(
      market.up_token_id,
      outcome="UP",
      condition_id=market.condition_id,
      market_slug=market.slug,
      market_end_time=market.end_time,
      gamma_price=market.up_price,
      slippage_notional=50.0,
    )

    print(report.metrics.bid_ask_spread)
    print(report.metrics.bid_depth.notional)
    print(report.metrics.ask_depth.notional)
    print(report.metrics.depth_ratio)
    print(report.metrics.buy_slippage.slippage)

if __name__ == '__main__':
    asyncio.run(main())
