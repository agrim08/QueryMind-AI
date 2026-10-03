"""FastAPI dependency — verifies Clerk JWT and loads the current user."""
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.models import User

bearer_scheme = HTTPBearer()

# Simple in-memory JWKS cache (refreshed on decode failure)
_jwks_cache: dict | None = None

_UNLIMITED = 999_999_999


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
        max_connections = _UNLIMITED
        max_queries_pm = _UNLIMITED
        max_designs_pm = _UNLIMITED
    elif is_pro:
        max_connections = 5
        max_queries_pm = _UNLIMITED
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


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.CLERK_JWKS_URL, timeout=10)
            resp.raise_for_status()
            _jwks_cache = resp.json()
    return _jwks_cache


async def _decode_token(token: str) -> dict:
    """Decode and verify the Clerk JWT, returning the full payload."""
    global _jwks_cache
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        jwks = await _get_jwks()
        return jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False},
            issuer=settings.CLERK_ISSUER,
        )
    except JWTError:
        # Retry once with a fresh JWKS in case the key rotated
        _jwks_cache = None
        try:
            jwks = await _get_jwks()
            return jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                options={"verify_aud": False},
                issuer=settings.CLERK_ISSUER,
            )
        except JWTError:
            raise credentials_exception


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Verify Clerk JWT, extract clerk_id, and return the DB user."""
    payload = await _decode_token(credentials.credentials)

    clerk_id: str | None = payload.get("sub")
    if clerk_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.clerk_id == clerk_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found — call /api/v1/users/sync first",
        )
    return user


async def get_current_user_with_entitlements(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> tuple[User, Entitlements]:
    """Like get_current_user but also returns the resolved Entitlements from the JWT."""
    payload = await _decode_token(credentials.credentials)

    clerk_id: str | None = payload.get("sub")
    if clerk_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.clerk_id == clerk_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found — call /api/v1/users/sync first",
        )

    entitlements = _build_entitlements(payload)
    return user, entitlements
