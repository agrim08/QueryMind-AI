"""FastAPI dependency — verifies Clerk JWT and loads the current user."""
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
