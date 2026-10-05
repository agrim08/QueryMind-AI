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

# Business setup (draft a description, extract definitions): once per connection, not per
# question. No thinking needed for summarising; the JSON output is bounded.
SETUP_TEMPERATURE = 0.2
SETUP_MAX_OUTPUT_TOKENS = 4096

# "gemini-embedding-002" does not exist (404); gemini-embedding-2 is the current GA model.
EMBEDDING_MODEL = "models/gemini-embedding-2"
EMBEDDING_DIMENSIONS = 768  # Matryoshka-truncated; stored as halfvec(768)
EMBED_BATCH_SIZE = 100  # max texts per embedding request
# Indexing: a rate-limited embedding batch waits this long (the per-minute window) and is
# retried once. Questions never wait: their one embedding call fails fast instead.
EMBED_RATE_LIMIT_WAIT_S = 60
# Tables and views indexed per connection (public schema and tables before views, largest
# first). Bounds embedding calls and storage for very wide databases.
MAX_INDEXED_TABLES = 1000

# Retrieval
# Schemas whose table documents fit this budget go to the model whole: no embedding call,
# no vector search, and no table can be missed. ~4 characters per token → ~12k tokens.
FULL_SCHEMA_CHAR_BUDGET = 48_000
# Larger schemas: the best tables by hybrid search, then the tables they reference. 12, not 6
# (2026-10-06, evals.retrieval on ~200-table schemas): 91% → 97% and 87% → 100% of questions
# get every table they need, for ~1,500 more schema characters; bridges and a second
# foreign-key hop added nothing on top.
RETRIEVAL_TOP_K_TABLES = 12
