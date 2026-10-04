import uuid
from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DesignPrompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class DBColumn(BaseModel):
    name: str = Field(description="Name of the column")
    type: str = Field(description="Standard SQL data type (e.g., VARCHAR, INTEGER, BOOLEAN)")
    constraints: Optional[str] = Field(default=None, description="Additional constraints (e.g., NOT NULL, UNIQUE)")
    isPrimary: bool = Field(default=False, description="True if this column is the primary key")
    isForeign: bool = Field(default=False, description="True if this column is a foreign key")

class DBTable(BaseModel):
    id: str = Field(description="A unique lowercase string identifier for the table, usually the same as the name")
    name: str = Field(description="The display name of the table")
    columns: List[DBColumn] = Field(description="List of columns in the table")

class DBEdge(BaseModel):
    id: str = Field(description="Unique ID for this edge/relationship")
    source: str = Field(description="The source table ID (the primary key table)")
    target: str = Field(description="The target table ID (the foreign key table)")
    label: Optional[str] = Field(default=None, description="Description of the relationship (e.g., 1:n or 1:1)")

class DBSchemaDesign(BaseModel):
    tables: List[DBTable] = Field(description="List of tables in the schema")
    edges: List[DBEdge] = Field(description="List of foreign key relationships between tables")

class GenerateSchemaRequest(BaseModel):
    prompt: DesignPrompt = Field(description="The natural language prompt describing the system to design")


class DesignLogResponse(BaseModel):
    """A saved design from the user's history."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prompt: str
    # Exposed as "schema_json" (the ORM column and API field); renamed in Python because
    # `schema_json` would shadow a BaseModel method.
    design: dict = Field(validation_alias="schema_json", serialization_alias="schema_json")
    created_at: datetime
