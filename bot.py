"""
Main bot entry point.

Startup sequence
----------------
1. Configure logging.
2. Initialise the database (create tables if absent).
3. Connect to the Polymarket CLOB.
4. Instantiate the strategy, market data service, order manager, audit logger.
5. Run the poll loop until interrupted (SIGINT / SIGTERM).

Changing the strategy
---------------------
Edit the ``_build_strategy()`` factory at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from core.client import PolymarketClient
from core.market_data import MarketDataService
from core.order_manager import OrderManager
from audit.logger import AuditLogger
from database.connection import init_db
from database.models import AuditAction, AuditResult
from strategy.base import BaseStrategy
from strategy.btc_5min import Btc5MinStrategy, StrategyConfig

# ------------------------------------------------------------------ #
# Logging setup
# ------------------------------------------------------------------ #

def _setup_logging() -> None:
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    level = logging.getLevelName(settings.log_level.upper())

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    handlers.append(file_handler)

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Bot
# ------------------------------------------------------------------ #

class PolybBot:
    """
    Orchestrates all components and runs the main polling loop.
    """

    def __init__(self, strategy: BaseStrategy) -> None:
        self._strategy = strategy
        self._audit = AuditLogger()
        self._client = PolymarketClient()
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        logger.info(
            "=== Polybot starting ===  dry_run=%s  poll_interval=%ss  strategy=%s",
            settings.dry_run,
            settings.poll_interval_seconds,
            self._strategy.name,
        )

        # --- infrastructure -----------------------------------------
        logger.info("Initialising database …")
        init_db()

        logger.info("Connecting to Polymarket …")
        await self._client.connect()

        # --- component wiring ---------------------------------------
        self._market_data = MarketDataService(
            client=self._client,
            condition_id=settings.btc_5min_condition_id,
            yes_token_id=settings.btc_5min_yes_token_id,
            no_token_id=settings.btc_5min_no_token_id,
        )
        self._order_manager = OrderManager(
            client=self._client,
            audit_logger=self._audit,
        )

        # --- signal handlers ----------------------------------------
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # --- strategy init ------------------------------------------
        self._strategy.on_start()
        self._audit.bot_start(
            details={
                "strategy": self._strategy.name,
                "dry_run": settings.dry_run,
                "poll_interval": settings.poll_interval_seconds,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # --- main loop ----------------------------------------------
        self._running = True
        try:
            while self._running:
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("Unhandled error in tick: %s", exc)
                    self._audit.error(str(exc), details={"context": "main_loop"})

                if self._running:
                    await asyncio.sleep(settings.poll_interval_seconds)
        finally:
            self._strategy.on_stop()
            self._audit.bot_stop(
                details={"stopped_at": datetime.now(timezone.utc).isoformat()}
            )
            await self._client.close()
            logger.info("=== Polybot stopped ===")

    def stop(self) -> None:
        logger.info("Stopping bot …")
        self._running = False

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    async def _tick(self) -> None:
        # 1. Fetch market data
        try:
            yes_data, no_data = await self._market_data.fetch()
        except Exception as exc:
            logger.error("Market data fetch failed: %s", exc)
            self._audit.record(
                action=AuditAction.MARKET_DATA_FETCH,
                result=AuditResult.FAILURE,
                error_message=str(exc),
            )
            return

        self._audit.record(
            action=AuditAction.MARKET_DATA_FETCH,
            result=AuditResult.SUCCESS,
            details={
                "yes_mid": yes_data.midpoint,
                "no_mid": no_data.midpoint,
                "yes_spread": yes_data.spread,
            },
        )

        # 2. Run strategy
        try:
            order_requests = self._strategy.on_tick(yes_data, no_data)
        except Exception as exc:
            logger.exception("Strategy raised an exception: %s", exc)
            self._audit.error(str(exc), details={"context": "strategy.on_tick"})
            return

        if order_requests:
            self._audit.strategy_signal(
                signal_name=self._strategy.name,
                details={"num_orders": len(order_requests)},
            )

        # 3. Execute orders
        for req in order_requests:
            result = await self._order_manager.place_order(req)
            try:
                self._strategy.on_order_result(req, result)
            except Exception as exc:
                logger.exception("Strategy.on_order_result raised: %s", exc)

    # ------------------------------------------------------------------ #
    # Signal handler
    # ------------------------------------------------------------------ #

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        logger.info("Shutdown signal received (%s) — stopping gracefully …", signum)
        self.stop()


# ------------------------------------------------------------------ #
# Strategy factory — edit here to swap strategies
# ------------------------------------------------------------------ #

def _build_strategy() -> BaseStrategy:
    """
    实例化并配置当前使用的交易策略。

    修改押注金额或触发条件请在此处调整 StrategyConfig 参数：
      - main_bet_usdc                  主方向押注金额（USDC）
      - hedge_bet_usdc                 对冲方向押注金额（USDC）
      - entry_seconds_before_settlement 距结算多少秒内入场
      - min_probability_threshold       触发阈值（0~1）
    """
    config = StrategyConfig(
        main_bet_usdc=5.0,
        hedge_bet_usdc=1.0,
        entry_seconds_before_settlement=60,
        min_probability_threshold=0.90,
    )
    return Btc5MinStrategy(config=config)


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    _setup_logging()
    strategy = _build_strategy()
    bot = PolybBot(strategy=strategy)
    asyncio.run(bot.start())
