import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.design import GenerateSchemaRequest, DBSchemaDesign
from app.services.schema_generator import generate_schema_from_prompt
from app.api.deps import get_current_user_with_entitlements, Entitlements
from app.core import errors
from app.db.session import get_db
from app.models.models import User, DesignLog

logger = logging.getLogger(__name__)
router = APIRouter()

_UNLIMITED = 999_999_999


@router.post("/generate-schema", response_model=DBSchemaDesign)
async def generate_schema(
    request: GenerateSchemaRequest,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
):
    """Generates a structured database schema and saves it to the user's design history."""
    current_user, entitlements = user_and_ent

    # Enforce monthly design limit for non-unlimited plans
    if entitlements.max_designs_pm < _UNLIMITED:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        monthly_count = await db.scalar(
            select(func.count(DesignLog.id)).where(
                DesignLog.user_id == current_user.id,
                DesignLog.created_at >= month_start,
            )
        )
        if (monthly_count or 0) >= entitlements.max_designs_pm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="DESIGN_LIMIT_REACHED",
            )

    try:
        schema = await generate_schema_from_prompt(request.prompt)

        log = DesignLog(
            user_id=current_user.id,
            prompt=request.prompt,
            schema_json=schema.model_dump(),
        )
        db.add(log)
        await db.commit()

        return schema
    except Exception as e:
        logger.error(f"Error generating schema: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history")
async def get_design_history(
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves the design history for the current user."""
    current_user, _ = user_and_ent
    try:
        result = await db.execute(
            select(DesignLog)
            .where(DesignLog.user_id == current_user.id)
            .order_by(DesignLog.created_at.desc())
        )
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Error fetching design history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch design history")


@router.get("/usage")
async def get_design_usage(
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
):
    """Returns current month's design usage and the plan limit."""
    current_user, entitlements = user_and_ent
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    count = await db.scalar(
        select(func.count(DesignLog.id)).where(
            DesignLog.user_id == current_user.id,
            DesignLog.created_at >= month_start,
        )
    )
    return {
        "used": count or 0,
        "limit": entitlements.max_designs_pm,
        "unlimited": entitlements.max_designs_pm >= _UNLIMITED,
    }
