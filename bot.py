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
import threading

import argparse
import urllib.parse

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from pprint import pprint


from config.settings import settings
from core.client import PolymarketClient
from core.event_market_resolver import EventMarketDataService, EventMarketResolver
from core.market_data import MarketDataService
from core.market_resolver import MarketResolver
from core.order_manager import OrderManager
from core.order_book import OrderBookService
from core.position_tracker import PositionTracker
from core.ws_market_feed import WsMarketFeed
from core.order_book import OrderBookAnalyzer
from dashboard.order_book_dashboard import SharedState,OrderBookDashboard
from audit.logger import AuditLogger
from database.connection import init_db
from database.models import AuditAction, AuditResult
from strategy.base import BaseStrategy
from strategy.btc_5min import Btc5MinStrategy, StrategyConfig
from strategy.btc_arb import BtcArbStrategy, ArbConfig
from strategy.multi_arb import MultiArbConfig, MultiArbStrategy
from strategy.slug_arb import SlugArbConfig, SlugArbStrategy


def _parse_election_slugs() -> list[str]:
    """将逗号分隔的 ELECTION_MARKET_SLUGS 字符串解析为列表。"""
    return [s.strip() for s in settings.election_market_slugs.split(",") if s.strip()]


def _parse_slug_arb_slugs() -> list[str]:
    """将逗号分隔的 SLUG_ARB_MARKET_SLUGS 字符串解析为列表。"""
    return [s.strip() for s in settings.slug_arb_market_slugs.split(",") if s.strip()]


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

    def __init__(self, strategy: Optional[BaseStrategy],shared_state=None) -> None:
        self._strategy = strategy
        self._audit = AuditLogger()
        self._client = PolymarketClient()
        self._running = False
        self._shared_state = shared_state
    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        active = []
        if self._strategy is not None:
            active.append(self._strategy.name)
        if settings.arb_enabled:
            active.append("btc_arb" + ("[观察]" if settings.arb_observe_mode else ""))
        if settings.election_arb_enabled:
            for _slug in _parse_election_slugs():
                active.append(
                    f"multi_arb:{_slug}"
                    + ("[观察]" if settings.election_arb_observe_mode else "")
                )
        if settings.slug_arb_enabled:
            for _slug in _parse_slug_arb_slugs():
                active.append(
                    f"slug_arb:{_slug}"
                    + ("[观察]" if settings.slug_arb_observe_mode else "")
                )
        logger.info(
            "=== Polybot starting ===  dry_run=%s  poll_interval=%ss  active_strategies=%s",
            settings.dry_run,
            settings.poll_interval_seconds,
            ", ".join(active) if active else "(none)",
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

        # --- 套利策略（可选）----------------------------------------
        self._arb_strategy: Optional[BtcArbStrategy] = None
        if settings.arb_enabled:
            arb_config = ArbConfig(
                min_merge_spread=settings.arb_min_merge_spread,
                min_split_spread=settings.arb_min_split_spread,
                max_trade_usdc=settings.arb_max_trade_usdc,
                base_trade_usdc=settings.arb_base_trade_usdc,
                cooldown_seconds=settings.arb_cooldown_seconds,
                liquidity_min_size=settings.arb_liquidity_min_size,
                slippage_tolerance=settings.arb_slippage_tolerance,
                estimated_gas_usdc=settings.arb_estimated_gas_usdc,
                observe_mode=settings.arb_observe_mode,
            )
            self._arb_strategy = BtcArbStrategy(
                client=self._client,
                order_manager=self._order_manager,
                config=arb_config,
            )
            self._arb_strategy.on_start()
            logger.info("套利策略已启用 (btc_arb)")
        else:
            logger.info("套利策略未启用（arb_enabled=False）")

        # --- 多选市场套利策略（可选，支持多个市场同时监听）-----------
        # 每个 slug 对应独立的 Resolver / DataService / Strategy 实例
        # list[tuple[EventMarketDataService, MultiArbStrategy]]
        self._election_components: list[tuple[EventMarketDataService, MultiArbStrategy]] = []
        if settings.election_arb_enabled:
            election_arb_config = MultiArbConfig(
                min_merge_spread=settings.election_arb_min_merge_spread,
                min_split_spread=settings.election_arb_min_split_spread,
                max_trade_usdc=settings.election_arb_max_trade_usdc,
                base_trade_usdc=settings.election_arb_base_trade_usdc,
                cooldown_seconds=settings.election_arb_cooldown_seconds,
                liquidity_min_size=settings.election_arb_liquidity_min_size,
                slippage_tolerance=settings.election_arb_slippage_tolerance,
                estimated_gas_usdc=settings.election_arb_estimated_gas_usdc,
                observe_mode=settings.election_arb_observe_mode,
            )
            for slug in _parse_election_slugs():
                e_resolver = EventMarketResolver(event_slug=slug, cache_ttl=300.0)
                e_data = EventMarketDataService(
                    resolver=e_resolver,
                    ws_feed=self._ws_feed,
                )
                try:
                    await e_data.ensure_subscribed()
                except Exception as exc:
                    logger.warning(
                        "多选市场 token 订阅失败（%s），将在首个 tick 时重试: %s",
                        slug, exc,
                    )
                e_arb = MultiArbStrategy(
                    event_slug=slug,
                    order_manager=self._order_manager,
                    config=election_arb_config,
                )
                e_arb.on_start()
                self._election_components.append((e_data, e_arb))
            logger.info(
                "多选市场套利策略已启用，共 %d 个市场: %s",
                len(self._election_components),
                ", ".join(_parse_election_slugs()),
            )
        else:
            logger.info("多选市场套利策略未启用（election_arb_enabled=False）")

        # --- 通用 Slug 套利策略（可选，支持多个市场同时监控）-----------
        self._slug_arb_strategy: Optional[SlugArbStrategy] = None
        if settings.slug_arb_enabled:
            slug_arb_slugs = _parse_slug_arb_slugs()
            if not slug_arb_slugs:
                logger.warning(
                    "slug_arb 已启用（SLUG_ARB_ENABLED=true）但 SLUG_ARB_MARKET_SLUGS 为空，"
                    "请在 .env 中配置至少一个 slug。"
                )
            else:
                slug_arb_config = SlugArbConfig(
                    min_merge_spread=settings.slug_arb_min_merge_spread,
                    min_split_spread=settings.slug_arb_min_split_spread,
                    max_trade_usdc=settings.slug_arb_max_trade_usdc,
                    base_trade_usdc=settings.slug_arb_base_trade_usdc,
                    cooldown_seconds=settings.slug_arb_cooldown_seconds,
                    liquidity_min_size=settings.slug_arb_liquidity_min_size,
                    slippage_tolerance=settings.slug_arb_slippage_tolerance,
                    estimated_gas_usdc=settings.slug_arb_estimated_gas_usdc,
                    observe_mode=settings.slug_arb_observe_mode,
                )
                self._slug_arb_strategy = SlugArbStrategy(
                    client=self._client,
                    order_manager=self._order_manager,
                    ws_feed=self._ws_feed,
                    slugs=slug_arb_slugs,
                    shared_state=self._shared_state,  # 传递 shared_state
                    config=slug_arb_config,
                )
                await self._slug_arb_strategy.on_start()
                logger.info(
                    "slug_arb 策略已启用，共 %d 个市场: %s",
                    len(slug_arb_slugs),
                    ", ".join(slug_arb_slugs),
                )
        else:
            logger.info("slug_arb 策略未启用（SLUG_ARB_ENABLED=false）")

        # --- signal handlers ----------------------------------------
        #signal.signal(signal.SIGINT, self._handle_shutdown)
        #signal.signal(signal.SIGTERM, self._handle_shutdown)
    
        # --- strategy init ------------------------------------------
        if self._strategy is not None:
            self._strategy.on_start()
        self._audit.bot_start(
            details={
                #"btc_5min_enabled": self._strategy is not None,
                "btc_5min_enabled": settings.btc_5min_enabled,
                "arb_enabled": settings.arb_enabled,
                "arb_observe_mode": settings.arb_observe_mode,
                "slug_arb_enabled": settings.slug_arb_enabled,
                "slug_arb_observe_mode": settings.slug_arb_observe_mode,
                "slug_arb_market_slugs": settings.slug_arb_market_slugs,
                "dry_run": settings.dry_run,
                "poll_interval": settings.poll_interval_seconds,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # --- main loop ----------------------------------------------
        self._running = True
        try:
            while self._running:
                if self._shared_state.shutdown:
                    break
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("Unhandled error in tick: %s", exc)
                    self._audit.error(str(exc), details={"context": "main_loop"})

                if self._running:
                    await asyncio.sleep(settings.poll_interval_seconds)
        finally:
            if self._strategy is not None:
                self._strategy.on_stop()
            if self._arb_strategy is not None:
                self._arb_strategy.on_stop()
            for _, e_arb in self._election_components:
                e_arb.on_stop()
            if self._slug_arb_strategy is not None:
                self._slug_arb_strategy.on_stop()
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


        # 1. BTC 5m 涨跌市场买卖策略
        if settings.btc_5min_enabled:
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
            self._shared_state.metrics_up = OrderBookAnalyzer.analyze(up_data,slippage_notional=50.0)
            self._shared_state.metrics_down = OrderBookAnalyzer.analyze(down_data,slippage_notional=50.0)
            logger.debug("up_data metrics: %s", self._shared_state.metrics_up)
            logger.debug("down_data metrics: %s", self._shared_state.metrics_down)
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

            # 以官方持仓为准，纠正 FAK 部分成交等导致的本地仓位偏差
            try:
                exchange_positions = await self._client.get_positions()
                self._position_tracker.sync_from_exchange(
                    condition_id=current_condition_id,
                    outcome_to_token_id={
                        "UP": up_data.token_id,
                        "DOWN": down_data.token_id,
                    },
                    positions=exchange_positions,
                )
                logger.debug(exchange_positions)
            except Exception as exc:
                logger.warning(
                    "[PositionTracker] 官方持仓同步失败，继续使用本地缓存: %s",
                    exc,
                )

        # 2a. 套利策略 tick（高优先级，在普通策略之前执行）
        if self._arb_strategy is not None:

            try:
                await self._arb_strategy.on_tick(up_data, down_data)
            except Exception as exc:
                logger.exception("[btc_arb] on_tick 异常: %s", exc)

        # 2b. 多选市场套利 tick（每个市场独立运行）
        for e_data, e_arb in self._election_components:
            try:
                outcomes = await e_data.fetch()
                await e_arb.on_tick(outcomes)
            except Exception as exc:
                logger.exception("[%s] on_tick 异常: %s", e_arb.name, exc)

        # 2c. 通用 Slug 套利 tick
        if self._slug_arb_strategy is not None:

            try:
                await self._slug_arb_strategy.on_tick()
            except Exception as exc:
                logger.exception("[slug_arb] on_tick 异常: %s", exc)

        # 2. 止损 / 止盈检查（在策略信号之前，优先清除局面）
        if self._strategy is not None and settings.strategy_stop_loss_pct > 0:
            await self._check_stop_loss(up_data, down_data)
        if self._strategy is not None and settings.strategy_take_profit_pct > 0:
            await self._check_take_profit(up_data, down_data)

        # 3. Run strategy
        if self._strategy is None:
            return
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
    # Take-profit
    # ------------------------------------------------------------------ #

    async def _check_take_profit(
        self,
        up_data: "MarketData",   # noqa: F821
        down_data: "MarketData",  # noqa: F821
    ) -> None:
        """
        Check every tracked open position for a take-profit trigger.

        Uses ``best_bid`` from the already-fetched ``MarketData`` snapshot.
        The bid price represents what we would receive if we sold immediately.
        """
        token_bid_map: dict[str, float | None] = {
            up_data.token_id:   up_data.best_bid,
            down_data.token_id: down_data.best_bid,
        }

        to_close = self._position_tracker.get_positions_to_take_profit(
            token_bid_map=token_bid_map,
            take_profit_pct=settings.strategy_take_profit_pct,
        )

        for pos, current_bid in to_close:
            logger.info(
                "[TakeProfit] 止盈平仓 %s %s — P&L=%.2f%%  平仓单价=%.4f  持股=%.4f",
                pos.outcome, pos.token_id[:8],
                pos.pnl_pct(current_bid), current_bid, pos.shares,
            )
            # 标记止盈进行中，避免同一持仓在后续 tick 重复触发
            self._position_tracker.mark_liquidating(pos.token_id)

            tp_order = pos.liquidation_order(
                current_bid=current_bid,
                order_type=settings.strategy_take_profit_order_type,
                strategy_tag="take_profit",
            )
            logger.info(
                "[TakeProfit] 下单平仓 — SELL %s %s @%.4f  size=%.4f USDC",
                settings.strategy_take_profit_order_type,
                pos.outcome, tp_order.price, tp_order.size,
            )
            try:
                result = await self._order_manager.place_order(tp_order)
                if result.success:
                    logger.info(
                        "[TakeProfit] 平仓成功 — local_id=%s exchange_id=%s",
                        result.local_order_id, result.exchange_order_id,
                    )
                    self._position_tracker.close_position(pos.token_id)
                else:
                    logger.error(
                        "[TakeProfit] 平仓失败 — %s  将在下一个 tick 重试",
                        result.error,
                    )
                    # 重置标志，允许下一个 tick 重试
                    pos.liquidating = False
            except Exception as exc:
                logger.exception("[TakeProfit] 平仓请求异常: %s", exc)
                pos.liquidating = False

    # ------------------------------------------------------------------ #
    # Signal handler
    # ------------------------------------------------------------------ #

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        logger.info("Shutdown signal received (%s) — stopping gracefully …", signum)
        self.stop()


# ------------------------------------------------------------------ #
# Strategy factory
# ------------------------------------------------------------------ #

def _build_strategy() -> Optional[BaseStrategy]:
    """
    根据 BTC_5MIN_ENABLED 配置决定是否实例化方向性策略。
    返回 None 表示该策略已关闭。
    """
    if not settings.btc_5min_enabled:
        logger.info("btc_5min 策略已关闭（BTC_5MIN_ENABLED=false）")
        return None

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

def run_bot(strategy,shared_state):
    try:
        bot = PolybBot(strategy=strategy, shared_state=shared_state)
        asyncio.run(bot.start())
    except Exception:
        import traceback
        traceback.print_exc()

def handle_shutdown(signum, frame):
    print(f"Received signal: {signum}")
    shared_state.shutdown = True

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polybot API function selector")
    parser.add_argument(
        "--mode",
        default=None,
        choices=["console", "tui"],
        help="Select the mode of operation: console or tui",
    )
    return parser.parse_args()

# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    args = _parse_args()
    _setup_logging()
    strategy = _build_strategy()
    if strategy is None and not settings.arb_enabled and not settings.election_arb_enabled and not settings.slug_arb_enabled:
        logger.error(
            "配置错误：btc_5min、btc_arb、election_arb 和 slug_arb 策略均已关闭。"
            "请在 .env 中至少启用一个策略。"
        )
        sys.exit(1)
    shared_state = SharedState()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    if args.mode == "tui":
        bot_thread = threading.Thread(
            target=run_bot,
            args=(strategy, shared_state),
        )
        bot_thread.start()
        app = OrderBookDashboard(shared_state)
        app.run()
    elif args.mode == "console":
        run_bot(strategy, shared_state)