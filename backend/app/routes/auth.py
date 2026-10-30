"""Local classroom token issuer with server-backed sessions and TOTP MFA."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import LoginRequest, MfaVerifyRequest, ProblemDetail, TokenOut
from app.security_identity import login_identity, verify_identity_mfa
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
