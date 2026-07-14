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

    所有策略通过 ``strategies`` 列表注入，bot 仅负责：
    - 初始化基础设施（client、ws_feed、order_manager 等）
    - 调用统一的 bind / on_start / on_tick / on_stop 生命周期
    """

    def __init__(self, strategies: list[BaseStrategy], shared_state=None) -> None:
        self._strategies = strategies
        self._audit = AuditLogger()
        self._client = PolymarketClient()
        self._running = False
        self._shared_state = shared_state
    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        active = [s.name for s in self._strategies] or ["(none)"]
        logger.info(
            "=== Polybot starting ===  dry_run=%s  poll_interval=%ss  active_strategies=%s",
            settings.dry_run,
            settings.poll_interval_seconds,
            ", ".join(active),
        )

        # --- infrastructure -----------------------------------------
        logger.info("Initialising database …")
        init_db()

        logger.info("Connecting to Polymarket …")
        await self._client.connect()

        # --- MarketResolver & WebSocket feed ------------------------
        # MarketResolver 和 MarketDataService 仅 btc_5min / btc_arb 需要；
        # slug_arb / multi_arb / election_arb 各自通过 EventMarketDataService 取数。
        need_btc_feed = settings.btc_5min_enabled or settings.arb_enabled

        self._ws_feed = WsMarketFeed()
        if need_btc_feed:
            resolver = MarketResolver(
                initial_timestamp=settings.btc_5min_start_timestamp,
            )
            initial_market = await resolver.get_active_market()
            initial_tokens = (initial_market.up_token_id, initial_market.down_token_id)
            self._market_data = MarketDataService(
                client=self._client,
                resolver=resolver,
                ws_feed=self._ws_feed,
            )
        else:
            initial_tokens = ()
            self._market_data = None

        self._ws_task = asyncio.create_task(
            self._ws_feed.run(*initial_tokens),
            name="ws_market_feed",
        )
        logger.info("WebSocket feed started — waiting for initial orderbook (up to 30s) …")
        ready = await self._ws_feed.wait_ready(timeout=30.0)
        if ready:
            logger.info("WebSocket feed is ready — using live data.")
        else:
            logger.warning("WebSocket feed not ready after 30s — will fall back to HTTP polling.")

        self._order_manager = OrderManager(
            client=self._client,
            audit_logger=self._audit,
        )
        self._position_tracker = PositionTracker()
        self._last_condition_id: str = ""

        # --- 统一 bind：将基础设施注入所有策略 -----------------------
        for strategy in self._strategies:
            strategy.bind(
                client=self._client,
                order_manager=self._order_manager,
                ws_feed=self._ws_feed,
                market_data_service=self._market_data,
                shared_state=self._shared_state,
            )

        # --- 统一 on_start ------------------------------------------
        for strategy in self._strategies:
            try:
                await strategy.on_start()
            except Exception as exc:
                logger.exception("[%s] on_start 异常: %s", strategy.name, exc)

        self._audit.bot_start(
            details={
                "strategies": active,
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
            for strategy in self._strategies:
                try:
                    strategy.on_stop()
                except Exception as exc:
                    logger.exception("[%s] on_stop 异常: %s", strategy.name, exc)
            self._audit.bot_stop(
                details={"stopped_at": datetime.now(timezone.utc).isoformat()}
            )
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
        """统一 tick：遍历所有策略，调用 on_tick() 并执行返回的订单。"""
        for strategy in self._strategies:
            try:
                order_requests = await strategy.on_tick()
            except Exception as exc:
                logger.exception("[%s] on_tick 异常: %s", strategy.name, exc)
                self._audit.error(str(exc), details={"context": f"{strategy.name}.on_tick"})
                continue

            # btc_5min 专属后处理：指标更新、市场切换检测、持仓同步、止损止盈
            if isinstance(strategy, Btc5MinStrategy):
                await self._post_tick_btc_5min(strategy)

            if not order_requests:
                continue

            self._audit.strategy_signal(
                signal_name=strategy.name,
                details={"num_orders": len(order_requests)},
            )
            for req in order_requests:
                result = await self._order_manager.place_order(req)
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
                    strategy.on_order_result(req, result)
                except Exception as exc:
                    logger.exception("[%s] on_order_result 异常: %s", strategy.name, exc)

    async def _post_tick_btc_5min(self, strategy: "Btc5MinStrategy") -> None:
        """btc_5min 的 tick 后处理：指标、市场切换、持仓同步、止损止盈。"""
        latest = strategy.latest_market_data
        if latest is None:
            return
        up_data, down_data = latest

        # 可视化指标
        self._shared_state.metrics_up = OrderBookAnalyzer.analyze(up_data, slippage_notional=50.0)
        self._shared_state.metrics_down = OrderBookAnalyzer.analyze(down_data, slippage_notional=50.0)

        # 市场切换检测
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
            self._position_tracker.clear_all()
        self._last_condition_id = current_condition_id

        self._audit.record(
            action=AuditAction.MARKET_DATA_FETCH,
            result=AuditResult.SUCCESS,
            details={
                "up_buy_price": up_data.best_ask,
                "up_sell_price": up_data.best_bid,
                "up_spread": up_data.spread,
                "down_buy_price": down_data.best_ask,
                "down_sell_price": down_data.best_bid,
                "down_spread": down_data.spread,
            },
        )

        # 持仓同步
        try:
            exchange_positions = await self._client.get_positions()
            self._position_tracker.sync_from_exchange(
                condition_id=current_condition_id,
                outcome_to_token_id={"UP": up_data.token_id, "DOWN": down_data.token_id},
                positions=exchange_positions,
            )
        except Exception as exc:
            logger.warning("[PositionTracker] 官方持仓同步失败，继续使用本地缓存: %s", exc)

        # 止损 / 止盈
        if settings.strategy_stop_loss_pct > 0:
            await self._check_stop_loss(up_data, down_data)
        if settings.strategy_take_profit_pct > 0:
            await self._check_take_profit(up_data, down_data)

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

def _build_strategies() -> list[BaseStrategy]:
    """
    按照 settings 构建并返回所有已启用策略的列表。
    策略此时只持有 config，infra 依赖在 bot.start() 中通过 bind() 注入。
    """
    strategies: list[BaseStrategy] = []

    if settings.btc_5min_enabled:
        logger.info("btc_5min 策略已启用（BTC_5MIN_ENABLED=true）")
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
        strategies.append(Btc5MinStrategy(config=config))

    if settings.arb_enabled:
        logger.info("btc_arb 策略已启用（ARB_ENABLED=true）")
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
        strategies.append(BtcArbStrategy(config=arb_config))

    if settings.election_arb_enabled:
        logger.info("election_arb 策略已启用（ELECTION_ARB_ENABLED=true）")
        election_config = MultiArbConfig(
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
            strategies.append(MultiArbStrategy(event_slug=slug, config=election_config))

    if settings.slug_arb_enabled:
        slug_arb_slugs = _parse_slug_arb_slugs()
        if not slug_arb_slugs:
            logger.warning(
                "slug_arb 已启用但 SLUG_ARB_MARKET_SLUGS 为空，请在 .env 中配置至少一个 slug。"
            )
        else:
            logger.info(
                "slug_arb 策略已启用（SLUG_ARB_ENABLED=true），共 %d 个市场: %s",
                len(slug_arb_slugs), ", ".join(slug_arb_slugs),
            )
            slug_config = SlugArbConfig(
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
            strategies.append(SlugArbStrategy(slugs=slug_arb_slugs, config=slug_config))

    if not strategies:
        logger.info("所有策略均已关闭。")

    return strategies

def run_bot(strategies: list[BaseStrategy], shared_state):
    try:
        bot = PolybBot(strategies=strategies, shared_state=shared_state)
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
    strategies = _build_strategies()
    if not strategies:
        logger.error(
            "配置错误：所有策略均已关闭，请在 .env 中至少启用一个策略。"
        )
        sys.exit(1)
    shared_state = SharedState()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    if args.mode == "tui":
        bot_thread = threading.Thread(
            target=run_bot,
            args=(strategies, shared_state),
        )
        bot_thread.start()
        app = OrderBookDashboard(shared_state)
        app.run()
    elif args.mode == "console":
        run_bot(strategies, shared_state)