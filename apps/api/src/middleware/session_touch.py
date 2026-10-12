import logging
from starlette.types import ASGIApp, Scope, Receive, Send
from src.auth.session import execute_session_touch

logger = logging.getLogger("tradepro.middleware.session_touch")

READ_ONLY_BROKER_PATHS = {
    "/api/v1/broker/connection",
    "/api/v1/broker/readiness",
    "/api/v1/broker/snapshots/latest",
}

class SessionTouchMiddleware:
    """
    Pure ASGI middleware that executes bounded post-request session activity touching.
    Crucial ordering guarantee:
    Executes AFTER downstream app returns, which means:
    - Route handling is complete.
    - Response headers and body have been produced and sent to client.
    - Request-scoped database dependencies (yield get_db) have finalized and closed.
    - No request read lock or transaction is held while executing the short-lived touch transaction.
    - Any touch failure cannot modify the already-sent HTTP response.

    Zero-write guarantee:
    Read-only broker endpoints are strictly protected from session touch mutations on ALL paths
    (including 200 success, 404 missing, 422 validation failure, and 401 unauthenticated).
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "").rstrip("/")

        # Strictly suppress session touch for read-only broker endpoints across all execution/error paths
        if method == "GET" and path in READ_ONLY_BROKER_PATHS:
            await self.app(scope, receive, send)
            return

        # Execute downstream request pipeline (including route handler and dependency teardown)
        await self.app(scope, receive, send)

        # Inspect request.state for any bounded session touch instruction
        state = scope.get("state")
        if state:
            touch_instruction = state.get("session_touch")
            if touch_instruction:
                try:
                    execute_session_touch(touch_instruction)
                except Exception as exc:
                    logger.error("Unexpected error in SessionTouchMiddleware: %s", exc, exc_info=True)
