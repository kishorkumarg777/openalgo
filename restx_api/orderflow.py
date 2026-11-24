"""REST endpoint serving locally recorded Order Flow ticks (/api/v1/orderflow)."""

from __future__ import annotations

import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import Schema, ValidationError, fields

from limiter import limiter
from services.orderflow_service import get_orderflow_ticks
from utils.logging import get_logger

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace("orderflow", description="Recorded Order Flow Tick Tape API")
logger = get_logger(__name__)


class OrderFlowRequestSchema(Schema):
    apikey = fields.Str(required=True)
    symbol = fields.Str(required=True)
    exchange = fields.Str(required=True)
    start_time = fields.Float(required=False, allow_none=True, load_default=None)
    end_time = fields.Float(required=False, allow_none=True, load_default=None)
    limit = fields.Int(required=False, load_default=100_000)


orderflow_schema = OrderFlowRequestSchema()


@api.route("", strict_slashes=False)
class OrderFlowResource(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Return recorded quote-classified order flow ticks for a symbol/time window."""
        try:
            payload = request.get_json(silent=True) or {}
            parsed = orderflow_schema.load(payload)
            _, response_data, status_code = get_orderflow_ticks(
                api_key=parsed["apikey"],
                symbol=parsed["symbol"],
                exchange=parsed["exchange"],
                start_time=parsed.get("start_time"),
                end_time=parsed.get("end_time"),
                limit=parsed.get("limit", 100_000),
            )
            return make_response(jsonify(response_data), status_code)
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as exc:
            logger.exception(f"Unexpected error in orderflow endpoint: {exc}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
