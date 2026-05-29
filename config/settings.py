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
    # Target market identifiers
    # ------------------------------------------------------------------ #
    # BTC 5-min up/down market on Polymarket
    btc_5min_condition_id: str = Field(
        description="Condition ID of the BTC 5-minute up/down market"
    )
    btc_5min_yes_token_id: str = Field(
        description="CLOB token ID for the YES outcome"
    )
    btc_5min_no_token_id: str = Field(
        description="CLOB token ID for the NO outcome"
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
