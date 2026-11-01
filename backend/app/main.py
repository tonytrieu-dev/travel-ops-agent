"""FastAPI application: routes, CORS for the frontend, and the ProblemDetail error boundary.

Domain rejections (BookingError) are rendered here as structured problem-details so every route
handler can stay thin and no stack trace or internal ever leaks to the client.
"""

from collections.abc import AsyncIterator
import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager
import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.compliance_monitoring import monitor_security_controls
from app.dbos_runtime import launch_dbos, shutdown_dbos
from app.rate_limit import RateLimitError
from app.repositories.booking_repository import BookingError
from app.repositories.trips_repository import TripError
from app.routes import auth, booking, connectors, security, slack, trips
from app.request_context import bind_correlation_id
from app.routes.connectors import ConnectorError
from app.schemas import ErrorCode, ProblemDetail
from app.security_admission import SecurityError
from app.security_identity import SESSION_COOKIE_NAME

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    launch_dbos()
    monitoring_task = asyncio.create_task(monitor_security_controls())
    try:
        yield
    finally:
        monitoring_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitoring_task
        shutdown_dbos()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Travel Agent API", version="0.1.0", lifespan=_lifespan)

    @app.middleware("http")
    async def protect_browser_writes(request: Request, call_next):
        browser_write = request.cookies.get(SESSION_COOKIE_NAME) or request.url.path.startswith("/api/auth/browser/")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"} and browser_write
            and request.headers.get("origin") != settings.frontend_origin
        ):
            return JSONResponse(status_code=403, content={"code": "forbidden", "detail": "trusted browser origin required"})
        return await call_next(request)

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get("x-correlation-id") or str(uuid4())
        with bind_correlation_id(request.state.correlation_id):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "Unhandled request exception correlation_id=%s",
                    request.state.correlation_id,
                )
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error"},
                )
        response.headers["x-correlation-id"] = request.state.correlation_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-correlation-id"],
    )
    app.include_router(booking.router)
    app.include_router(auth.router)
    app.include_router(connectors.router)
    app.include_router(security.router)
    app.include_router(slack.router)
    app.include_router(trips.router)

    @app.exception_handler(BookingError)
    @app.exception_handler(ConnectorError)
    @app.exception_handler(TripError)
    async def _render_domain_error(
        request: Request, error: BookingError | ConnectorError | TripError
    ) -> JSONResponse:
        problem = ProblemDetail(code=error.code, detail=error.detail)
        return JSONResponse(status_code=error.status_code, content=problem.model_dump(mode="json"))

    @app.exception_handler(RateLimitError)
    async def _render_rate_limit_error(request: Request, error: RateLimitError) -> JSONResponse:
        problem = ProblemDetail(code=error.code, detail=error.detail)
        return JSONResponse(
            status_code=429,
            content=problem.model_dump(mode="json"),
            headers={"Retry-After": str(error.retry_after_seconds)},
        )

    @app.exception_handler(SecurityError)
    async def _render_security_error(request: Request, error: SecurityError) -> JSONResponse:
        problem = ProblemDetail(code=ErrorCode.FORBIDDEN, detail=error.detail)
        return JSONResponse(status_code=error.status_code, content=problem.model_dump(mode="json"))

    @app.exception_handler(401)
    async def _render_authentication_error(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        problem = ProblemDetail(
            code=ErrorCode.AUTHENTICATION_REQUIRED,
            detail=str(error.detail),
        )
        return JSONResponse(
            status_code=error.status_code,
            content=problem.model_dump(mode="json"),
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _render_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        field_errors = "; ".join(
            f"{'.'.join(str(part) for part in violation['loc'])}: {violation['msg']}"
            for violation in error.errors()
        )
        problem = ProblemDetail(code=ErrorCode.VALIDATION_ERROR, detail=field_errors)
        return JSONResponse(status_code=422, content=problem.model_dump(mode="json"))

    return app


app = create_app()
