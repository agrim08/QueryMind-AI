"""Schema Designer endpoints — generate a design, list history, report usage."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import UNLIMITED, CurrentUser, Plan, require_design_quota
from app.core import errors
from app.core.exceptions import UpstreamFailure
from app.db.session import DbSession
from app.models.models import DesignLog
from app.schemas.design import DBSchemaDesign, DesignLogResponse, GenerateSchemaRequest
from app.schemas.schemas import UsageResponse
from app.services import usage
from app.services.schema_generator import generate_schema_from_prompt

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/generate-schema", response_model=DBSchemaDesign, dependencies=[Depends(require_design_quota)])
async def generate_schema(request: GenerateSchemaRequest, user: CurrentUser, db: DbSession) -> DBSchemaDesign:
    """Generate a schema design and save it to the user's history (counts toward the plan)."""
    try:
        schema = await generate_schema_from_prompt(request.prompt)
    except Exception:
        logger.exception("Schema generation failed for user %s", user.id)
        raise UpstreamFailure(errors.DESIGN_FAILED) from None

    db.add(DesignLog(user_id=user.id, prompt=request.prompt, schema_json=schema.model_dump()))
    await db.commit()
    return schema


@router.get("/history", response_model=list[DesignLogResponse])
async def get_design_history(user: CurrentUser, db: DbSession) -> list[DesignLogResponse]:
    """The user's saved designs, newest first."""
    logs = await db.scalars(
        select(DesignLog).where(DesignLog.user_id == user.id).order_by(DesignLog.created_at.desc())
    )
    return [DesignLogResponse.model_validate(log) for log in logs]


@router.get("/usage", response_model=UsageResponse)
async def get_design_usage(user: CurrentUser, plan: Plan, db: DbSession) -> UsageResponse:
    """This month's design count and the plan limit."""
    return UsageResponse(
        used=await usage.count_designs_this_month(db, user.id),
        limit=plan.max_designs_pm,
        unlimited=plan.max_designs_pm >= UNLIMITED,
    )
