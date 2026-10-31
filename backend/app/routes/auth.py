"""Local classroom token issuer with server-backed sessions and TOTP MFA."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import get_security_context
from app.schemas import BrowserLoginOut, LoginRequest, MfaVerifyRequest, ProblemDetail, TokenOut
from app.security_identity import SESSION_COOKIE_NAME, SecurityContext, login_identity, verify_identity_mfa
from app import security_posture as posture
from app.rate_limit import enforce_login_rate_limit, enforce_request_rate_limit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/config")
async def auth_config() -> dict[str, bool]:
    return {"enforced": posture.enforcement_active()}


@router.post(
    "/login",
    response_model=TokenOut,
    responses={401: {"model": ProblemDetail}, 429: {"model": ProblemDetail}},
    dependencies=[Depends(enforce_login_rate_limit)],
)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    return await login_identity(session, body, request)


@router.post("/mfa", response_model=TokenOut, dependencies=[Depends(enforce_request_rate_limit)])
async def verify_mfa(
    body: MfaVerifyRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    return await verify_identity_mfa(session, body, request)


def _browser_response(result: TokenOut, request: Request, response: Response) -> BrowserLoginOut:
    response.headers["Cache-Control"] = "no-store"
    if not result.mfa_required:
        local_http = request.url.scheme == "http" and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        response.set_cookie(
            SESSION_COOKIE_NAME, result.access_token, httponly=True,
            secure=not local_http, samesite="strict", path="/api",
            expires=result.expires_at,
        )
    return BrowserLoginOut(**result.model_dump())


@router.post("/browser/login", response_model=BrowserLoginOut, dependencies=[Depends(enforce_login_rate_limit)])
async def browser_login(
    body: LoginRequest, request: Request, response: Response, session: AsyncSession = Depends(get_session),
) -> BrowserLoginOut:
    return _browser_response(await login_identity(session, body, request), request, response)


@router.post("/browser/mfa", response_model=BrowserLoginOut, dependencies=[Depends(enforce_request_rate_limit)])
async def browser_verify_mfa(
    body: MfaVerifyRequest, request: Request, response: Response, session: AsyncSession = Depends(get_session),
) -> BrowserLoginOut:
    return _browser_response(await verify_identity_mfa(session, body, request), request, response)


@router.get("/browser/session")
async def browser_session(response: Response, context: SecurityContext = Depends(get_security_context)) -> dict[str, str | None]:
    response.headers["Cache-Control"] = "no-store"
    return {"session_id": context.session_id}
