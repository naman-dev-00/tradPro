import logging
from starlette.types import ASGIApp, Scope, Receive, Send
from src.auth.session import execute_session_touch

logger = logging.getLogger("tradepro.middleware.session_touch")

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
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
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
