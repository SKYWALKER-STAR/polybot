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
    poll_interval_seconds: int = Field(default=30, ge=5)

    # Hard ceiling on a single order's USDC notional value.
    max_order_size_usdc: float = Field(default=50.0, gt=0)

    # Maximum total open exposure per market side (YES / NO).
    max_position_size_usdc: float = Field(default=200.0, gt=0)

    # ------------------------------------------------------------------ #
    # Strategy — BTC 5-min
    # ------------------------------------------------------------------ #
    # FOK 市价单押注金额（USDC）
    strategy_fok_bet_usdc: float = Field(default=1.0, ge=0)

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

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="logs/polybot.log")

    @field_validator("private_key")
    @classmethod
    def _strip_0x(cls, v: str) -> str:
        return v.removeprefix("0x")


# Module-level singleton — import this everywhere.
settings = Settings()  # type: ignore[call-arg]
