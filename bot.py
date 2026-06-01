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
from core.market_resolver import MarketResolver
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from core.ws_market_feed import WsMarketFeed
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

    handlers: list[logging.Handler] = []

    if settings.log_console:
        handlers.append(logging.StreamHandler(sys.stdout))

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
        resolver = MarketResolver(
            initial_timestamp=settings.btc_5min_start_timestamp,
        )

        # --- WebSocket market feed (runs as background task) --------
        # Resolve token IDs once so we can subscribe before the first tick.
        initial_market = await resolver.get_active_market()
        self._ws_feed = WsMarketFeed()
        self._ws_task = asyncio.create_task(
            self._ws_feed.run(initial_market.up_token_id, initial_market.down_token_id),
            name="ws_market_feed",
        )
        logger.info(
            "WebSocket feed started — waiting for initial orderbook (up to 30s) …"
        )
        ready = await self._ws_feed.wait_ready(timeout=30.0)
        if ready:
            logger.info("WebSocket feed is ready — using live data.")
        else:
            logger.warning(
                "WebSocket feed not ready after 30s — will fall back to HTTP polling."
            )

        self._market_data = MarketDataService(
            client=self._client,
            resolver=resolver,
            ws_feed=self._ws_feed,
        )
        self._order_manager = OrderManager(
            client=self._client,
            audit_logger=self._audit,
        )
        self._position_tracker = PositionTracker()
        self._last_condition_id: str = ""  # 用于检测市场切换

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
            # Cancel WS background task
            if hasattr(self, "_ws_task") and not self._ws_task.done():
                self._ws_feed.stop()
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
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
            up_data, down_data = await self._market_data.fetch()
        except Exception as exc:
            logger.error("Market data fetch failed: %s", exc)
            self._audit.record(
                action=AuditAction.MARKET_DATA_FETCH,
                result=AuditResult.FAILURE,
                error_message=str(exc),
            )
            return

        # 检测市场切换 — 新市场开始时撤销上一个市场的所有残留挂单
        current_condition_id = up_data.condition_id
        if self._last_condition_id and self._last_condition_id != current_condition_id:
            logger.info(
                "市场已切换 %s → %s，开始撤销旧市场残留挂单 …",
                self._last_condition_id, current_condition_id,
            )
            try:
                cancelled = await self._order_manager.cancel_orders_for_condition(
                    self._last_condition_id
                )
                logger.info("旧市场挂单已撤销 %d 笔", cancelled)
            except Exception as exc:
                logger.warning("撤销旧市场挂单时出错: %s", exc)
            # 市场切换时清空持仓记录（旧市场结算，持仓已关闭）
            self._position_tracker.clear_all()
        self._last_condition_id = current_condition_id

        self._audit.record(
            action=AuditAction.MARKET_DATA_FETCH,
            result=AuditResult.SUCCESS,
            details={
                "up_buy_price":    up_data.best_ask,    # 买入 UP 时支付的价格（最低 ask）
                "up_sell_price":   up_data.best_bid,    # 卖出 UP 时收到的价格（最高 bid）
                "up_spread":       up_data.spread,
                "down_buy_price":  down_data.best_ask,  # 买入 DOWN 时支付的价格（最低 ask）
                "down_sell_price": down_data.best_bid,  # 卖出 DOWN 时收到的价格（最高 bid）
                "down_spread":     down_data.spread,
            },
        )

        # 2. 止损检查（在策略信号之前，优先清除局面)
        if settings.strategy_stop_loss_pct > 0:
            await self._check_stop_loss(up_data, down_data)

        # 3. Run strategy
        try:
            order_requests = self._strategy.on_tick(up_data, down_data)
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
            # 成功提交的买入订单，记录到持仓跟踪器中
            if result.success and req.side == "BUY":
                self._position_tracker.record_fill(
                    token_id=req.token_id,
                    outcome=req.outcome,
                    condition_id=req.condition_id,
                    market_slug=req.market_slug,
                    price=req.price,
                    size_usdc=req.size,
                )
            try:
                self._strategy.on_order_result(req, result)
            except Exception as exc:
                logger.exception("Strategy.on_order_result raised: %s", exc)

    # ------------------------------------------------------------------ #
    # Stop-loss
    # ------------------------------------------------------------------ #

    async def _check_stop_loss(
        self,
        up_data: "MarketData",   # noqa: F821
        down_data: "MarketData",  # noqa: F821
    ) -> None:
        """
        Check every tracked open position for a stop-loss trigger.

        Uses WebSocket ``best_bid`` from the already-fetched ``MarketData``
        snapshot — this is the fastest available price (in-memory, sub-ms).
        The bid price represents what we would receive if we sold immediately.
        """
        token_bid_map: dict[str, float | None] = {
            up_data.token_id:   up_data.best_bid,
            down_data.token_id: down_data.best_bid,
        }

        to_liquidate = self._position_tracker.get_positions_to_liquidate(
            token_bid_map=token_bid_map,
            stop_loss_pct=settings.strategy_stop_loss_pct,
        )

        for pos, current_bid in to_liquidate:
            logger.warning(
                "[StopLoss] 平仓 %s %s — P&L=%.2f%%  平仓单价=%.4f  持股=%.4f",
                pos.outcome, pos.token_id[:8],
                pos.pnl_pct(current_bid), current_bid, pos.shares,
            )
            # 标记止损进行中，避免同一持仓在后续 tick 重复触发
            self._position_tracker.mark_liquidating(pos.token_id)

            liq_order = pos.liquidation_order(
                current_bid=current_bid,
                order_type=settings.strategy_stop_loss_order_type,
                strategy_tag="stop_loss",
            )
            logger.info(
                "[StopLoss] 下单平仓 — SELL GTC %s @%.4f  size=%.4f USDC",
                pos.outcome, liq_order.price, liq_order.size,
            )
            try:
                result = await self._order_manager.place_order(liq_order)
                if result.success:
                    logger.info(
                        "[StopLoss] 平仓成功 — local_id=%s exchange_id=%s",
                        result.local_order_id, result.exchange_order_id,
                    )
                    # 平仓成功后删除记录；若下单失败则保留记录以便下一 tick 重试
                    self._position_tracker.close_position(pos.token_id)
                else:
                    logger.error(
                        "[StopLoss] 平仓失败 — %s  将在下一个 tick 重试",
                        result.error,
                    )
                    # 重置 liquidating 标志，允许下一个 tick 重试
                    pos.liquidating = False
            except Exception as exc:
                logger.exception("[StopLoss] 平仓请求异常: %s", exc)
                pos.liquidating = False

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
      - fok_bet_usdc                   FOK 市价单押注金额（USDC）
      - gtc_bet_usdc                   GTC 限价单押注金额（USDC）
      - hedge_bet_usdc                 对冲方向押注金额（USDC）
      - entry_seconds_before_settlement 距结算多少秒内入场
      - target_price                    目标价格（0~1）
      - price_tolerance                 价格容忍带（0~1，如 0.03 = ±3%）
    """
    config = StrategyConfig(
        fok_bet_usdc=settings.strategy_fok_bet_usdc,
        fak_bet_usdc=settings.strategy_fak_bet_usdc,
        gtc_bet_usdc=settings.strategy_gtc_bet_usdc,
        hedge_bet_usdc=settings.strategy_hedge_bet_usdc,
        entry_seconds_before_settlement=settings.strategy_entry_seconds,
        target_price=settings.strategy_target_price,
        price_tolerance=settings.strategy_price_tolerance,
        limit_price_offset=settings.strategy_limit_price_offset,
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
