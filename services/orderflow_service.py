"""Service layer for querying locally recorded Order Flow classified ticks."""

from __future__ import annotations

from typing import Any

from database.auth_db import verify_api_key
from database.orderflow_tick_db import get_orderflow_recorder
from utils.logging import get_logger

logger = get_logger(__name__)


def get_orderflow_ticks(
    api_key: str,
    symbol: str,
    exchange: str,
    *,
    start_time: float | None = None,
    end_time: float | None = None,
    limit: int = 100_000,
) -> tuple[bool, dict[str, Any], int]:
    """Authenticate API key and return recorded classified ticks for a symbol."""
    if not api_key:
        return False, {"status": "error", "message": "Missing API Key"}, 401

    user_id = verify_api_key(api_key)
    if not user_id:
        return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403

    sym = (symbol or "").strip().upper()
    exch = (exchange or "").strip().upper()
    if not sym or not exch:
        return False, {"status": "error", "message": "symbol and exchange are required"}, 400

    try:
        recorder = get_orderflow_recorder()
        ticks = recorder.query_ticks(
            sym,
            exch,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return (
            True,
            {
                "status": "success",
                "data": {
                    "symbol": sym,
                    "exchange": exch,
                    "recording": recorder.enabled,
                    "count": len(ticks),
                    "ticks": ticks,
                },
            },
            200,
        )
    except Exception as exc:
        logger.exception(f"[OrderFlowService] Error querying ticks for {exch}:{sym}: {exc}")
        return False, {"status": "error", "message": "Failed to read recorded order flow ticks"}, 500
