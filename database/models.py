"""
ORM models — all persistent state lives here.

Tables
------
orders          Lifecycle of every order sent to Polymarket.
trades          Individual fill events associated with an order.
market_snapshots  Point-in-time order-book snapshots (for back-testing / analysis).
audit_logs      Immutable record of every action the bot takes.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as PgEnum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ #
# Enumerations
# ------------------------------------------------------------------ #

class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"       # created locally, not yet confirmed by exchange
    OPEN = "OPEN"             # live on the book
    PARTIAL = "PARTIAL"       # partially filled, still on the book
    FILLED = "FILLED"         # fully matched
    CANCELLED = "CANCELLED"   # cancelled by bot or exchange
    EXPIRED = "EXPIRED"       # GTD order past its expiry
    FAILED = "FAILED"         # exchange rejected the order


class OrderType(str, enum.Enum):
    GTC = "GTC"   # Good-Till-Cancelled  (default)
    FOK = "FOK"   # Fill-Or-Kill
    GTD = "GTD"   # Good-Till-Date


class AuditAction(str, enum.Enum):
    PLACE_ORDER = "PLACE_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    CANCEL_ALL = "CANCEL_ALL"
    MARKET_DATA_FETCH = "MARKET_DATA_FETCH"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    BOT_START = "BOT_START"
    BOT_STOP = "BOT_STOP"
    ERROR = "ERROR"


class AuditResult(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"   # e.g. dry-run suppressed execution


# ------------------------------------------------------------------ #
# Base
# ------------------------------------------------------------------ #

class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ #
# orders
# ------------------------------------------------------------------ #

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Exchange-assigned identifier; NULL until the exchange confirms creation.
    polymarket_order_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)

    # Market identifiers
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    market_slug: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Order parameters
    side: Mapped[OrderSide] = mapped_column(PgEnum(OrderSide, name="order_side"), nullable=False)
    outcome: Mapped[Outcome] = mapped_column(PgEnum(Outcome, name="outcome"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(
        PgEnum(OrderType, name="order_type"), nullable=False, default=OrderType.GTC
    )

    # price is 0–1 for binary markets (probability expressed as a decimal)
    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    size_matched: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)

    status: Mapped[OrderStatus] = mapped_column(
        PgEnum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.PENDING
    )

    # Was this order created during a dry-run (never sent to exchange)?
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Optional strategy tag for analysis
    strategy_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    trades: Mapped[list[Trade]] = relationship("Trade", back_populates="order", lazy="select")

    __table_args__ = (
        Index("ix_orders_condition_status", "condition_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} side={self.side} outcome={self.outcome} "
            f"price={self.price} size={self.size} status={self.status}>"
        )


# ------------------------------------------------------------------ #
# trades  (individual fill events)
# ------------------------------------------------------------------ #

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    polymarket_trade_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )

    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    order: Mapped[Order] = relationship("Order", back_populates="trades")

    def __repr__(self) -> str:
        return f"<Trade id={self.id} order_id={self.order_id} price={self.price} size={self.size}>"


# ------------------------------------------------------------------ #
# market_snapshots
# ------------------------------------------------------------------ #

class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Best bid / ask for the token (UP or DOWN)
    best_bid: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    spread: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    last_trade_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    midpoint: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    # Full order-book payload for deeper analysis / replay
    raw_order_book: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        Index("ix_snapshots_condition_captured", "condition_id", "captured_at"),
    )


# ------------------------------------------------------------------ #
# audit_logs
# ------------------------------------------------------------------ #

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    action: Mapped[AuditAction] = mapped_column(
        PgEnum(AuditAction, name="audit_action"), nullable=False, index=True
    )
    result: Mapped[AuditResult] = mapped_column(
        PgEnum(AuditResult, name="audit_result"), nullable=False
    )

    # Free-form context: order IDs, market IDs, strategy output, etc.
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Human-readable error description when result == FAILURE
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action} result={self.result}>"
