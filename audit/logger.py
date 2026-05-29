"""
Audit logger — writes an immutable record of every bot action to
the ``audit_logs`` table in PostgreSQL.

Design notes
------------
* Every call is fire-and-forget from the caller's perspective.
* Failures in the audit writer are logged but NEVER propagate to the
  caller — audit failure must not disrupt trading.
* All timestamps are stored in UTC.
* Sensitive values (private keys, passwords) MUST NOT appear in ``details``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from database.connection import get_session
from database.models import AuditAction, AuditLog, AuditResult

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    Thread-safe audit recorder.

    Usage
    -----
    ::

        audit = AuditLogger()
        audit.record(
            action=AuditAction.PLACE_ORDER,
            result=AuditResult.SUCCESS,
            details={"order_id": 42, "price": 0.54},
        )
    """

    def record(
        self,
        action: AuditAction,
        result: AuditResult,
        details: Optional[dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Persist one audit record.

        Parameters
        ----------
        action:        What the bot tried to do.
        result:        SUCCESS, FAILURE, or SKIPPED (dry-run).
        details:       Free-form dict with contextual data (JSON-serialisable).
        error_message: Human-readable error description when result == FAILURE.
        """
        try:
            with get_session() as session:
                session.add(
                    AuditLog(
                        action=action,
                        result=result,
                        details=details,
                        error_message=error_message,
                        created_at=datetime.now(timezone.utc),
                    )
                )
            self._emit_to_log(action, result, details, error_message)
        except Exception as exc:
            # Audit failure must never crash the caller.
            logger.error("AuditLogger failed to persist record: %s", exc)

    # ------------------------------------------------------------------ #
    # Convenience wrappers
    # ------------------------------------------------------------------ #

    def bot_start(self, details: Optional[dict[str, Any]] = None) -> None:
        self.record(AuditAction.BOT_START, AuditResult.SUCCESS, details=details)

    def bot_stop(self, details: Optional[dict[str, Any]] = None) -> None:
        self.record(AuditAction.BOT_STOP, AuditResult.SUCCESS, details=details)

    def strategy_signal(
        self,
        signal_name: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        payload = {"signal": signal_name, **(details or {})}
        self.record(AuditAction.STRATEGY_SIGNAL, AuditResult.SUCCESS, details=payload)

    def error(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        self.record(
            AuditAction.ERROR,
            AuditResult.FAILURE,
            details=details,
            error_message=message,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _emit_to_log(
        action: AuditAction,
        result: AuditResult,
        details: Optional[dict[str, Any]],
        error_message: Optional[str],
    ) -> None:
        """Mirror the audit event to the application log for easy tailing."""
        if result == AuditResult.FAILURE:
            logger.warning(
                "AUDIT [%s] result=%s error=%s details=%s",
                action.value, result.value, error_message, details,
            )
        else:
            logger.info(
                "AUDIT [%s] result=%s details=%s",
                action.value, result.value, details,
            )
