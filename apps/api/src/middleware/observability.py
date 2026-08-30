import re
import time
import uuid
import logging
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("tradepro.observability")

REQUEST_ID_REGEX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

def sanitize_and_validate_request_id(client_id: Optional[str]) -> str:
    if client_id and REQUEST_ID_REGEX.match(client_id):
        return client_id
    return str(uuid.uuid4())

def get_duration_bucket(elapsed_seconds: float) -> str:
    elapsed_ms = elapsed_seconds * 1000.0
    if elapsed_ms < 10.0:
        return "<10ms"
    elif elapsed_ms < 50.0:
        return "10-50ms"
    elif elapsed_ms < 200.0:
        return "50-200ms"
    else:
        return ">200ms"

class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        raw_req_id = request.headers.get("X-Request-ID")
        request_id = sanitize_and_validate_request_id(raw_req_id)

        # Store in request state for downstream handlers
        request.state.request_id = request_id

        start_time = time.monotonic()
        response: Optional[Response] = None

        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.monotonic() - start_time
            duration_bucket = get_duration_bucket(elapsed)
            # Log bounded structured failure
            logger.error(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "route": "unresolved",
                    "status_code": 500,
                    "duration_bucket": duration_bucket,
                },
            )
            raise

        elapsed = time.monotonic() - start_time
        duration_bucket = get_duration_bucket(elapsed)

        # Extract normalized route template safely
        route_template = "unresolved"
        route_obj = request.scope.get("route")
        if route_obj and hasattr(route_obj, "path"):
            route_template = route_obj.path
        elif request.scope.get("endpoint"):
            # If endpoint function exists
            route_template = request.scope.get("path", "unresolved")

        # Emit structured log
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": route_template,
                "status_code": response.status_code,
                "duration_bucket": duration_bucket,
            },
        )

        response.headers["X-Request-ID"] = request_id
        return response
