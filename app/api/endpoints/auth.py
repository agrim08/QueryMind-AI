"""Auth endpoint — mirrors the signed-in Clerk user into the application database."""
from fastapi import APIRouter

from app.api.deps import TokenClaims
from app.db.session import DbSession
from app.schemas.schemas import UserResponse, UserSyncRequest
from app.services import users as user_service

router = APIRouter()


@router.post("/sync", response_model=UserResponse)
async def sync_user(payload: UserSyncRequest, claims: TokenClaims, db: DbSession) -> UserResponse:
    """Create or update the current user. Idempotent — safe to call on every sign-in.

    The Clerk user id comes from the verified token, so a caller can only ever
    create or update their own record.
    """
    user = await user_service.upsert_from_clerk(
        db,
        clerk_id=claims["sub"],
        email=payload.email,
        full_name=payload.full_name,
        avatar_url=payload.avatar_url,
    )
    return UserResponse.model_validate(user)
