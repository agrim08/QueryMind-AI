"""FastAPI dependencies: authentication, plan entitlements, ownership and quotas.

Use the Annotated aliases in endpoint signatures:

    async def endpoint(user: CurrentUser, plan: Plan, db: DbSession): ...

and plan limits and rate limits as route dependencies:

    @router.post("/", dependencies=[Depends(require_design_quota), Depends(rate_limited("design", 5))])

The monthly question limit is enforced by services.query_meter.reserve instead, because it
must be checked and recorded atomically.

FastAPI resolves each dependency once per request, so the JWT is verified once even
when several dependencies need its claims.
"""
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select

from app.core.config import settings
from app.core import errors
from app.core.exceptions import LimitReached, RateLimited
from app.core.rate_limit import RateLimiter
from app.db.session import DbSession
from app.models.models import DBConnection, User
from app.services import connections as connection_service
from app.services import usage

bearer_scheme = HTTPBearer()

# Simple in-memory JWKS cache (refreshed on decode failure)
_jwks_cache: dict | None = None

UNLIMITED = 999_999_999


@dataclass(frozen=True)
class Entitlements:
    """Resolved plan limits parsed from the Clerk JWT `fea` claim."""
    features: frozenset
    is_pro: bool
    is_team: bool
    max_connections: int   # 1 (free) / 5 (pro) / unlimited (team)
    max_queries_pm: int    # 50 (free) / unlimited (pro+)
    max_designs_pm: int    # 1 (free) / 6 (pro) / unlimited (team)
    csv_export: bool
    pdf_export: bool


def _build_entitlements(payload: dict) -> Entitlements:
    """Parse Clerk JWT features array into a typed Entitlements object."""
    raw_fea = payload.get("fea") or []
    features: frozenset = frozenset(raw_fea)

    is_team = "team_tier" in features
    is_pro = is_team or "pro_tier" in features

    if is_team:
        max_connections = UNLIMITED
        max_queries_pm = UNLIMITED
        max_designs_pm = UNLIMITED
    elif is_pro:
        max_connections = 5
        max_queries_pm = UNLIMITED
        max_designs_pm = 6
    else:
        max_connections = 1
        max_queries_pm = 50
        max_designs_pm = 1

    return Entitlements(
        features=features,
        is_pro=is_pro,
        is_team=is_team,
        max_connections=max_connections,
        max_queries_pm=max_queries_pm,
        max_designs_pm=max_designs_pm,
        csv_export="csv_export" in features or is_pro,
        pdf_export="pdf_export" in features or is_team,
    )


# ── Token verification ────────────────────────────────────────────────────────

def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.CLERK_JWKS_URL, timeout=10)
            resp.raise_for_status()
            _jwks_cache = resp.json()
    return _jwks_cache


def _decode(token: str, jwks: dict) -> dict:
    return jwt.decode(
        token, jwks, algorithms=["RS256"], options={"verify_aud": False}, issuer=settings.CLERK_ISSUER
    )


async def _decode_token(token: str) -> dict:
    """Decode and verify the Clerk JWT, retrying once with fresh keys after a rotation."""
    global _jwks_cache
    try:
        return _decode(token, await _get_jwks())
    except JWTError:
        _jwks_cache = None
        try:
            return _decode(token, await _get_jwks())
        except JWTError:
            raise _unauthorized() from None


async def get_token_claims(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict[str, Any]:
    """Verified JWT claims. `sub` (the Clerk user id) is guaranteed to be present."""
    claims = await _decode_token(credentials.credentials)
    if not claims.get("sub"):
        raise _unauthorized()
    return claims


TokenClaims = Annotated[dict[str, Any], Depends(get_token_claims)]


# ── Current user and plan ─────────────────────────────────────────────────────

async def get_current_user(claims: TokenClaims, db: DbSession) -> User:
    user = await db.scalar(select(User).where(User.clerk_id == claims["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found — call /api/v1/users/sync first",
        )
    return user


def get_entitlements(claims: TokenClaims) -> Entitlements:
    return _build_entitlements(claims)


CurrentUser = Annotated[User, Depends(get_current_user)]
Plan = Annotated[Entitlements, Depends(get_entitlements)]


# ── Ownership ─────────────────────────────────────────────────────────────────

async def get_owned_connection(connection_id: uuid.UUID, user: CurrentUser, db: DbSession) -> DBConnection:
    """The `{connection_id}` path parameter, resolved only if the current user owns it."""
    return await connection_service.get_for_user(db, user.id, connection_id)


OwnedConnection = Annotated[DBConnection, Depends(get_owned_connection)]


# ── Plan limits (use as route dependencies) ───────────────────────────────────

async def require_design_quota(user: CurrentUser, plan: Plan, db: DbSession) -> None:
    if plan.max_designs_pm < UNLIMITED and await usage.count_designs_this_month(db, user.id) >= plan.max_designs_pm:
        raise LimitReached("DESIGN_LIMIT_REACHED")


async def require_connection_slot(user: CurrentUser, plan: Plan, db: DbSession) -> None:
    if await usage.count_connections(db, user.id) >= plan.max_connections:
        raise LimitReached("CONNECTION_LIMIT_REACHED")


# ── Rate limits (use as route dependencies) ───────────────────────────────────

_rate_limiters: dict[str, RateLimiter] = {}


def rate_limited(action: str, per_minute: int):
    """Dependency allowing each user at most `per_minute` calls of `action` per minute."""
    limiter = _rate_limiters.setdefault(action, RateLimiter(per_minute, 60))

    async def check(user: CurrentUser) -> None:
        if not limiter.allow(str(user.id)):
            raise RateLimited(errors.TOO_MANY_REQUESTS)

    return check
