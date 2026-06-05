"""
Global configuration loaded from environment variables / .env file.
All sensitive values (private key, API credentials) must never be hard-coded.
"""

from __future__ import annotations

from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # Polymarket SDK
    # ------------------------------------------------------------------ #
    # Wallet private key (hex, with or without 0x prefix)
    private_key: str = Field(description="Ethereum private key for order signing")

    # Optional: Polymarket wallet address. When set, the SDK uses this as the
    # trading wallet while the private key acts as the signer only.
    wallet_address: Optional[str] = Field(
        default=None,
        description="Polymarket wallet address (defaults to signer address when unset)",
    )

    # ------------------------------------------------------------------ #
    # Target market — BTC 5-minute up/down
    # ------------------------------------------------------------------ #
    # 初始市场的 slug Unix 时间戳，例如 btc-updown-5m-1780073700 中的 1780073700。
    # MarketResolver 会从此时间戳出发，自动跟踪后续每个 5-min 市场。
    btc_5min_start_timestamp: int = Field(
        description="Initial Unix timestamp for the BTC 5-min market slug (e.g. 1780073700)"
    )

    # ------------------------------------------------------------------ #
    # Polymarket CLOB endpoint
    # ------------------------------------------------------------------ #
    clob_host: str = Field(
        default="https://clob.polymarket.com",
        description="Polymarket CLOB API base URL",
    )
    chain_id: int = Field(
        default=137,
        description="EVM chain ID (137 = Polygon mainnet)",
    )

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str = Field(
        description="PostgreSQL DSN, e.g. postgresql+psycopg2://user:pass@host:5432/polybot"
    )

    # ------------------------------------------------------------------ #
    # Bot runtime
    # ------------------------------------------------------------------ #
    # Safety switch: when True the bot evaluates strategy but never sends
    # real orders to the exchange.
    dry_run: bool = Field(default=True)

    # How often (in seconds) the main loop wakes up and polls market data.
    # With WebSocket feed active, market data is read from memory — 1s is safe.
    poll_interval_seconds: float = Field(default=1.0, ge=0.1)

    # Hard ceiling on a single order's USDC notional value.
    max_order_size_usdc: float = Field(default=50.0, gt=0)

    # Maximum total open exposure per market side (YES / NO).
    max_position_size_usdc: float = Field(default=200.0, gt=0)

    # ------------------------------------------------------------------ #
    # Strategy — BTC 5-min
    # ------------------------------------------------------------------ #
    # 是否启用方向性策略（btc_5min）
    btc_5min_enabled: bool = Field(default=True)

    # FOK 市价单押注金额（USDC）
    strategy_fok_bet_usdc: float = Field(default=1.0, ge=0)

    # FAK 市价单押注金额（USDC）—— 尽量成交，剩余部分取消
    strategy_fak_bet_usdc: float = Field(default=0.0, ge=0)

    # GTC 限价单押注金额（USDC）
    strategy_gtc_bet_usdc: float = Field(default=1.0, ge=0)

    # 对冲方向押注金额（USDC）
    strategy_hedge_bet_usdc: float = Field(default=1.0, ge=0)

    # 距结算多少秒内开始入场检查
    strategy_entry_seconds: int = Field(default=60, ge=5)

    # 目标入场价格（0~1），best_ask 落在此价格 ±tolerance 内时触发
    strategy_target_price: float = Field(default=0.80, gt=0, lt=1)

    # 价格容忍带（0~1），例如 0.03 = ±3%
    strategy_price_tolerance: float = Field(default=0.03, gt=0, lt=1)

    # 限价单偏移：实际下单价 = best_ask - offset（0.0 = 直接贴 best_ask 挂单）
    strategy_limit_price_offset: float = Field(default=0.0, ge=0, lt=1)
    # 止损单类型：GTC（挂单等待成交）/ FOK（全部成交或取消）/ FAK（尽量成交剩余取消）
    strategy_stop_loss_order_type: str = Field(default="GTC")
    # 止损阈值（%，正数）：持仓亏损达到此百分比时立即平仓。
    # 例如 20.0 = 亏损 20% 时触发止损。设为 0 可关闭止损。
    strategy_stop_loss_pct: float = Field(default=0.0, ge=0, lt=100)

    # 止盈单类型：GTC（挂单等待成交）/ FOK（全部成交或取消）/ FAK（尽量成交剩余取消）
    strategy_take_profit_order_type: str = Field(default="GTC")
    # 止盈阈值（%，正数）：持仓盈利达到此百分比时止盈平仓。
    # 例如 20.0 = 盈利 20% 时触发止盈。设为 0 可关闭止盈。
    strategy_take_profit_pct: float = Field(default=0.0, ge=0, lt=1000)

    # ------------------------------------------------------------------ #
    # Strategy — BTC Arb (Split/Merge 无风险价差套利)
    # ------------------------------------------------------------------ #

    # 是否启用套利策略（与 btc_5min 策略可同时运行）
    arb_enabled: bool = Field(default=False)

    # 观察模式：仅打印订单簿市价和套利机会，不执行任何交易
    # 适合上线前的市场观察与阈值调优，与 ARB_ENABLED 同时开启时生效
    arb_observe_mode: bool = Field(default=False)

    # Merge 套利触发最低价差（例如 0.008 = ask_YES + ask_NO ≤ 0.992 时触发）
    arb_min_merge_spread: float = Field(default=0.008, gt=0, lt=1)

    # Split 套利触发最低价差（例如 0.008 = bid_YES + bid_NO ≥ 1.008 时触发）
    arb_min_split_spread: float = Field(default=0.008, gt=0, lt=1)

    # 单次套利最大 USDC 规模
    arb_max_trade_usdc: float = Field(default=100.0, gt=0)

    # 单次套利基础 USDC 规模（受深度限制后可能更小）
    arb_base_trade_usdc: float = Field(default=50.0, gt=0)

    # 两次套利之间的冷却时间（秒）
    arb_cooldown_seconds: float = Field(default=3.0, ge=0)

    # 订单簿最小可用深度（shares）
    arb_liquidity_min_size: float = Field(default=10.0, gt=0)

    # FOK 买/卖单滑点容忍（USDC 价格偏移）
    arb_slippage_tolerance: float = Field(default=0.002, ge=0, lt=0.1)

    # Gas 费用估算（USDC），用于净利润门槛过滤
    arb_estimated_gas_usdc: float = Field(default=0.005, ge=0)

    # ------------------------------------------------------------------ #
    # Election / multi-choice market monitoring
    # ------------------------------------------------------------------ #

    # 是否启用多选市场套利监听（election_arb）
    election_arb_enabled: bool = Field(default=False)

    # 观察模式：仅打印套利机会，不执行任何交易。建议首次启用时保持 True。
    election_arb_observe_mode: bool = Field(default=True)

    # 要监听的 Polymarket 事件 slug 列表（逗号分隔），即事件 URL 最后一段路径，例如：
    #   https://polymarket.com/event/democratic-presidential-nominee-2028
    #                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    # 支持同时监听多个市场，逗号分隔即可，例如：
    #   ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028,oscar-best-picture-2027
    election_market_slugs: list[str] = Field(
        default=["democratic-presidential-nominee-2028"],
        description="Comma-separated Polymarket event slugs for multi-choice market monitoring",
    )

    @field_validator("election_market_slugs", mode="before")
    @classmethod
    def _parse_slugs(cls, v: object) -> list[str]:
        """Accept both a JSON list and a plain comma-separated string."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    # Merge 套利触发阈值：YES_ask + NO_ask ≤ 1 - min_merge_spread
    election_arb_min_merge_spread: float = Field(default=0.005, gt=0, lt=1)

    # Split 套利触发阈值：YES_bid + NO_bid ≥ 1 + min_split_spread
    election_arb_min_split_spread: float = Field(default=0.005, gt=0, lt=1)

    # 单次套利最大 USDC 规模
    election_arb_max_trade_usdc: float = Field(default=100.0, gt=0)

    # 单次套利基础 USDC 规模（受深度限制后可能更小）
    election_arb_base_trade_usdc: float = Field(default=20.0, gt=0)

    # 两次套利之间的冷却时间（秒）
    election_arb_cooldown_seconds: float = Field(default=5.0, ge=0)

    # 订单簿最小可用深度（shares）
    election_arb_liquidity_min_size: float = Field(default=5.0, gt=0)

    # FOK 买/卖单滑点容忍
    election_arb_slippage_tolerance: float = Field(default=0.002, ge=0, lt=0.1)

    # Gas 费用估算（USDC），用于净利润门槛过滤
    election_arb_estimated_gas_usdc: float = Field(default=0.005, ge=0)

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="logs/polybot.log")
    log_console: bool = Field(default=True)   # 是否将日志输出到终端

    @field_validator("private_key")
    @classmethod
    def _strip_0x(cls, v: str) -> str:
        return v.removeprefix("0x")


# Module-level singleton — import this everywhere.
settings = Settings()  # type: ignore[call-arg]
