"""SMC Stock Screener REST API (/api/v1/screener/*).

Strictly read-only endpoints mirroring stock_screener's 8 LuxAlgo-compatible SMC screeners,
multi-timeframe checks (LTF confirmation + HTF OB confluence), and single-stock Explain diagnostic.

Security matches OpenAlgo's API contract:
  * Every data endpoint verifies the OpenAlgo API key via verify_api_key.
  * source='api' additionally requires an active broker session via get_auth_token_broker.
  * Jobs are scoped to the API key owner so one user cannot inspect or stop another user's job.
  * API keys are passed in JSON POST bodies (never in URLs).
"""
from __future__ import annotations

import os
from typing import Any, Dict

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import Schema, ValidationError, fields, validate

from database.auth_db import get_auth_token_broker, verify_api_key
from limiter import limiter
from services.screener import registry
from services.screener.data_provider import (
    ScreenerApiError,
    ScreenerAuthError,
    ScreenerNetworkError,
    create_provider,
)
from services.screener.engine import explain_symbol, get_metadata, job_manager
from utils.logging import get_logger

META_RATE_LIMIT = os.getenv("SCREENER_META_RATE_LIMIT", "60 per minute")
START_RATE_LIMIT = os.getenv("SCREENER_START_RATE_LIMIT", "30 per minute")
POLL_RATE_LIMIT = os.getenv("SCREENER_POLL_RATE_LIMIT", "180 per minute")

api = Namespace("screener", description="SMC Stock Screener API")
logger = get_logger(__name__)


class StartScanSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    timeframe = fields.Str(required=True, validate=validate.Length(min=1, max=16))
    scan = fields.Dict(load_default=dict)
    strategy_settings = fields.Dict(load_default=dict)


class JobActionSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    job_id = fields.Str(required=True, validate=validate.Length(min=1, max=64))


class ExplainSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    strategy = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    timeframe = fields.Str(required=True, validate=validate.Length(min=1, max=16))
    scan = fields.Dict(load_default=dict)
    strategy_settings = fields.Dict(load_default=dict)


start_schema = StartScanSchema()
job_action_schema = JobActionSchema()
explain_schema = ExplainSchema()


def _error(message: str, status: int = 400, errors: list[str] | None = None):
    payload: Dict[str, Any] = {"status": "error", "message": message}
    if errors:
        payload["errors"] = errors
    return make_response(jsonify(payload), status)


@api.route("/meta", strict_slashes=False)
class ScreenerMeta(Resource):
    @limiter.limit(META_RATE_LIMIT)
    def get(self):
        """Return all 8 SMC screeners, their setting schemas, defaults, timeframes, and universes."""
        return make_response(jsonify(get_metadata()), 200)


@api.route("/start", strict_slashes=False)
class ScreenerStart(Resource):
    @limiter.limit(START_RATE_LIMIT)
    def post(self):
        """Validate settings and start a background SMC scan job."""
        try:
            data = start_schema.load(request.json or {})
        except ValidationError as err:
            return _error("Invalid screener request payload.", 400, [str(err.messages)])

        api_key = data["apikey"]
        owner_id = verify_api_key(api_key)
        if owner_id is None:
            return _error("Invalid OpenAlgo API key.", 403)

        clean_scan, clean_strat, errors, warnings = registry.validate_run_settings(
            data["strategy"],
            data["timeframe"],
            data.get("scan"),
            data.get("strategy_settings"),
        )
        if errors:
            return _error(errors[0], 400, errors)

        auth_token = feed_token = broker = None
        if clean_scan["source"] == "api":
            auth_token, feed_token, broker = get_auth_token_broker(
                api_key, include_feed_token=True
            )
            if auth_token is None or not broker:
                return _error(
                    "Broker API scan requires an active broker login. Log in to your broker "
                    "or switch Data Source to Historify DB.",
                    403,
                )

        try:
            job = job_manager.start_scan(
                owner_key=str(owner_id),
                scan=clean_scan,
                strat_settings=clean_strat,
                broker=broker,
                auth_token=auth_token,
                feed_token=feed_token,
            )
        except ScreenerAuthError as e:
            return _error(str(e), 403)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to start SMC screener job")
            return _error("Could not start screener job. Check server logs.", 500)

        snap = job.snapshot()
        snap["warnings"] = warnings
        return make_response(jsonify({"status": "success", "data": snap}), 200)


@api.route("/status", strict_slashes=False)
class ScreenerStatus(Resource):
    @limiter.limit(POLL_RATE_LIMIT)
    def post(self):
        """Poll live progress, stats, and matched rows of a running or finished scan job."""
        try:
            data = job_action_schema.load(request.json or {})
        except ValidationError as err:
            return _error("Invalid status request.", 400, [str(err.messages)])

        owner_id = verify_api_key(data["apikey"])
        if owner_id is None:
            return _error("Invalid OpenAlgo API key.", 403)

        job = job_manager.get_job(data["job_id"], str(owner_id))
        if job is None:
            return _error("Scan job not found or expired.", 404)

        return make_response(jsonify({"status": "success", "data": job.snapshot()}), 200)


@api.route("/stop", strict_slashes=False)
class ScreenerStop(Resource):
    @limiter.limit(START_RATE_LIMIT)
    def post(self):
        """Cancel a running scan job cleanly, preserving results collected so far."""
        try:
            data = job_action_schema.load(request.json or {})
        except ValidationError as err:
            return _error("Invalid stop request.", 400, [str(err.messages)])

        owner_id = verify_api_key(data["apikey"])
        if owner_id is None:
            return _error("Invalid OpenAlgo API key.", 403)

        job = job_manager.stop_job(data["job_id"], str(owner_id))
        if job is None:
            return _error("Scan job not found or expired.", 404)

        return make_response(jsonify({"status": "success", "data": job.snapshot()}), 200)


@api.route("/explain", strict_slashes=False)
class ScreenerExplain(Resource):
    @limiter.limit(START_RATE_LIMIT)
    def post(self):
        """Run one screener on one symbol with rejected rows enabled and return every active OB & +BB."""
        try:
            data = explain_schema.load(request.json or {})
        except ValidationError as err:
            return _error("Invalid explain request.", 400, [str(err.messages)])

        api_key = data["apikey"]
        owner_id = verify_api_key(api_key)
        if owner_id is None:
            return _error("Invalid OpenAlgo API key.", 403)

        clean_scan, clean_strat, errors, _ = registry.validate_run_settings(
            data["strategy"],
            data["timeframe"],
            data.get("scan"),
            data.get("strategy_settings"),
            require_universe_symbols=False,
        )
        if errors:
            return _error(errors[0], 400, errors)

        auth_token = feed_token = broker = None
        if clean_scan["source"] == "api":
            auth_token, feed_token, broker = get_auth_token_broker(
                api_key, include_feed_token=True
            )
            if auth_token is None or not broker:
                return _error(
                    "Broker API requires an active broker login. Log in to your broker "
                    "or switch Data Source to Historify DB.",
                    403,
                )

        try:
            provider = create_provider(
                clean_scan["source"],
                broker=broker,
                auth_token=auth_token,
                feed_token=feed_token,
            )
            result = explain_symbol(
                provider,
                data["symbol"],
                clean_scan,
                clean_strat,
            )
        except ScreenerAuthError as e:
            return _error(str(e), 403)
        except (ValueError, ScreenerApiError, ScreenerNetworkError) as e:
            return _error(str(e), 400)
        except Exception:  # noqa: BLE001
            logger.exception("SMC screener explain failed for %s", data.get("symbol"))
            return _error("Could not explain symbol. Check server logs.", 500)

        return make_response(jsonify({"status": "success", "data": result}), 200)
