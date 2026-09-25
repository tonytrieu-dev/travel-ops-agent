"""FastAPI application: routes, CORS for the frontend, and the ProblemDetail error boundary.

Domain rejections (BookingError) are rendered here as structured problem-details so every route
handler can stay thin and no stack trace or internal ever leaks to the client.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.dbos_runtime import launch_dbos, shutdown_dbos
from app.rate_limit import RateLimitError
from app.repositories.booking_repository import BookingError
from app.repositories.trips_repository import TripError
from app.routes import auth, booking, connectors, security, service, slack, trips
from app.request_context import set_correlation_id
from app.routes.connectors import ConnectorError
from app.schemas import ErrorCode, ProblemDetail
from app.security import SecurityError


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    launch_dbos()
    yield
    shutdown_dbos()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Travel Agent API", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get("x-correlation-id") or str(uuid4())
        set_correlation_id(request.state.correlation_id)
        response = await call_next(request)
        response.headers["x-correlation-id"] = request.state.correlation_id
        return response
    app.include_router(booking.router)
    app.include_router(auth.router)
    app.include_router(connectors.router)
    app.include_router(security.router)
    app.include_router(service.router)
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
