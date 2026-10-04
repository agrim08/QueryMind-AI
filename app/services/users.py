"""User records mirrored from Clerk."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import User


async def upsert_from_clerk(
    session: AsyncSession,
    clerk_id: str,
    email: str,
    full_name: str | None,
    avatar_url: str | None,
) -> User:
    """Create or update the user. `clerk_id` must come from a verified token, never a request body."""
    user = await session.scalar(select(User).where(User.clerk_id == clerk_id))
    if user is None:
        user = User(clerk_id=clerk_id, email=email, full_name=full_name, avatar_url=avatar_url)
        session.add(user)
    else:
        user.email = email
        if full_name is not None:
            user.full_name = full_name
        if avatar_url is not None:
            user.avatar_url = avatar_url
    await session.commit()
    await session.refresh(user)
    return user
