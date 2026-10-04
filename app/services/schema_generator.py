"""Schema Designer generator: a plain-English description → a structured DBSchemaDesign."""
from google.genai import types as genai_types

from app.core.ai_config import GENERATION_MODEL
from app.schemas.design import DBSchemaDesign
from app.services.genai_client import get_genai_client

SYSTEM_PROMPT = """You are an expert Database Architect.
Listen to the user's requirements for an application, and design a robust PostgreSQL relational database schema.
You MUST output structured JSON matching the provided schema.

Guidelines:
1. Tables must have an 'id' (typically UUID or SERIAL) as a primary key.
2. If two tables are related, include a foreign key column in the child table (e.g. `user_id`) and set `isForeign: true`.
3. Add edges representing the relationships, using the table IDs as source and target.
4. Provide appropriate SQL types (VARCHAR(255), TIMESTAMP, INTEGER, BOOLEAN, etc.) and constraints (NOT NULL, UNIQUE).
"""


async def generate_schema_from_prompt(prompt: str) -> DBSchemaDesign:
    """Generate a schema design; Gemini's structured output is validated by Pydantic."""
    response = await get_genai_client().aio.models.generate_content(
        model=GENERATION_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=DBSchemaDesign,
        ),
    )
    if isinstance(response.parsed, DBSchemaDesign):
        return response.parsed
    # Fallback when the SDK couldn't parse: validate the raw JSON ourselves.
    return DBSchemaDesign.model_validate_json(response.text)
