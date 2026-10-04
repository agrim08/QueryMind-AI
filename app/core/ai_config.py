"""Every AI-related constant, defined once (see .claude/rules/ai-pipeline.md §1).

Changing EMBEDDING_MODEL or EMBEDDING_DIMENSIONS means every connection must be
re-indexed, and the `schema_elements.embedding` column dimension must match.
"""

GENERATION_MODEL = "gemini-2.5-flash"

# SQL generation. On 2.5 models max_output_tokens includes thinking tokens, so the budget
# leaves at least 1024 tokens for the SQL itself. Thinking is capped (not disabled): it
# helps multi-table joins, but uncapped it delays the first SQL token. Measure changes
# with the eval suite (backend/evals).
SQL_TEMPERATURE = 0.1
SQL_THINKING_BUDGET = 1024
SQL_MAX_OUTPUT_TOKENS = 2048

# "gemini-embedding-002" does not exist (404); gemini-embedding-2 is the current GA model.
EMBEDDING_MODEL = "models/gemini-embedding-2"
EMBEDDING_DIMENSIONS = 768  # Matryoshka-truncated; stored as halfvec(768)
EMBED_BATCH_SIZE = 100  # max texts per embedding request

# Retrieval
# Schemas whose table documents fit this budget go to the model whole: no embedding call,
# no vector search, and no table can be missed. ~4 characters per token → ~12k tokens.
FULL_SCHEMA_CHAR_BUDGET = 48_000
# Larger schemas: the closest tables by vector search, then the tables they reference.
RETRIEVAL_TOP_K_TABLES = 6
