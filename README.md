# QueryMind — Backend

**Your database, in plain English.** Connect a PostgreSQL database, ask a question in English, and get
read-only SQL plus live results, streamed as they're produced. A second feature, the **Schema
Designer**, turns a plain-English description into an ER diagram with SQL and PDF export.

This repo is the FastAPI backend. The Next.js frontend is
[agrim08/query-mind-fe](https://github.com/agrim08/query-mind-fe).

[![FastAPI](https://img.shields.io/badge/FastAPI-async-080909?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11-080909?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![Gemini](https://img.shields.io/badge/Gemini-2.5%20Flash-080909?style=flat-square&logo=google&logoColor=white)](https://ai.google.dev)
[![pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-080909?style=flat-square&logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector)
[![Neon](https://img.shields.io/badge/Neon-Postgres-080909?style=flat-square)](https://neon.tech)

---

## How a question is answered

Every step streams a status event to the browser over Server-Sent Events, so the UI is never frozen.

```
POST /api/v1/query  (Clerk JWT)
  │
  ├─ checks: token, monthly plan limit, the connection belongs to this user, it has been indexed
  │          (all before any AI call, so a refused request costs nothing)
  │
  ├─ retrieve the tables the model sees
  │     small schema (fits ~48k chars)  → every table, no embedding call
  │     larger schema                   → embed the question, top 6 tables by cosine distance
  │                                       (pgvector), plus every table they reference by foreign key
  │
  ├─ generate   Gemini 2.5 Flash streams SQL token by token (thinking capped, output bounded)
  ├─ parse      strip Markdown fences; split "-- Assumption:" lines or "-- Cannot answer: <reason>"
  ├─ validate   one SELECT, keyword blocklist, side-effecting functions blocked, known tables only
  ├─ execute    fresh connection, READ ONLY transaction, 10 s statement_timeout, server-side cursor
  │             reading at most 501 rows (500 shown + 1 to detect truncation), never committed
  └─ results → done → history row (the usage meter)
```

## How a database is indexed

`POST /api/v1/connections/{id}/index` streams progress while it:

1. **Takes a claim** on the connection (atomic `UPDATE … RETURNING`, 15-minute expiry) so two runs
   can't overlap; the claim is released in `finally`, even if the client disconnects.
2. **Reads the structure from the Postgres catalogs** in 4 queries, inside a read-only transaction
   with a 15 s timeout, whatever the schema size: tables, views, materialized views and foreign
   tables in every schema the role can read (partitions fold into their parent; system, platform and
   extension objects are skipped), with columns, keys, comments, enum labels and row estimates.
3. **Adds example values only for categories**: low-variety text columns (≤ 25 distinct values),
   from `pg_stats` or a bounded read of tables with ≤ 200 rows. Columns that look personal (emails,
   phones, passwords, names…) never contribute values.
4. **Writes one document per table**, embeds them 100 per call (`gemini-embedding-2`, 768 dimensions),
   and replaces the connection's rows in `schema_elements` (`halfvec(768)`) **in one transaction**
   with the "indexed" flag: a failed run never leaves a half-written index.

Example document:

```
Table: invoice (~412 rows)
Columns:
- invoice_id (integer) PRIMARY KEY
- billing_country (varchar(40)) values: 'USA', 'Canada', 'Brazil', 'France' (+20 more)
- total (numeric(10,2)) NOT NULL
Foreign Keys:
- (customer_id) -> customer(customer_id)
```

## Safety model

QueryMind runs AI-written SQL on people's own databases, so no single layer is trusted:

| Layer | What it stops |
|---|---|
| Prompt | Asks for one read-only `SELECT` over the listed tables. Helpful, never relied on |
| Validator ([`sql_validator.py`](app/services/sql_validator.py)) | Multiple statements, writes and DDL, `SELECT … INTO`, row locks, `pg_sleep` / `pg_terminate_backend` / `dblink` and similar, tables the model wasn't given |
| Read-only transaction | Any write that gets past the validator: Postgres itself refuses it |
| Timeout and row cap | Runaway queries and huge results (the cap is enforced while reading, not after) |
| Fresh connection per query, no pool | One user's connection being reused for another |
| Outbound host guard | Private, loopback, link-local and cloud-metadata addresses (SSRF) |

Plus: identity comes only from the verified Clerk JWT (`sub`); every query on app data filters by
`user_id` in SQL, and another user's id returns 404; connection strings are Fernet-encrypted at rest
and never logged or returned; driver errors are mapped to plain-English messages with credentials
redacted.

## Measuring accuracy

[`evals/`](evals/) scores the real pipeline on public sample databases (Chinook, Pagila): 68
questions with hand-written gold SQL, tagged by type (joins, rankings, dates, typos, questions that
must be declined…). A question passes when the generated query returns the same rows as the gold
query. The first run found a validator bug that rejected every correct `EXTRACT(YEAR FROM …)` query.

It's designed for Gemini's free tier (20 generations a day): answers are cached by a hash of the
exact request, so only prompt or retrieval changes cost quota, and a daily scheduled run resumes
where it stopped. Scores and setup: [`evals/README.md`](evals/README.md).

## Tech stack

| Area | Choice |
|---|---|
| API | FastAPI (async), Pydantic v2 |
| App database | Neon PostgreSQL via SQLAlchemy 2.0 async + asyncpg, Alembic migrations |
| Vectors | pgvector in the same database (`halfvec(768)`, cosine distance), plus `pg_trgm` |
| AI | `gemini-2.5-flash` (SQL, schema designs), `gemini-embedding-2` (embeddings), one shared async client |
| Auth and plans | Clerk JWT (RS256 via cached JWKS); plan features from the token's `fea` claim, enforced here |
| Streaming | Server-Sent Events |
| Encryption | Fernet for stored connection strings |

## Project structure

```
app/
  main.py                 app, CORS, exception handlers, routers under /api/v1
  api/
    deps.py               auth, plan entitlements, ownership and quota dependencies
    endpoints/            auth (user sync), connections, query, design — HTTP only
  services/
    query_pipeline.py     retrieve → generate → parse → validate → execute
    schema_introspection.py, schema_indexer.py, schema_store.py, schema_retriever.py
    sql_generator.py, sql_validator.py, query_executor.py, target_db.py
    embeddings.py, genai_client.py, schema_generator.py, connections.py, users.py, usage.py
  core/                   settings, AI constants, errors and messages, SSE helper, encryption
  models/ schemas/        SQLAlchemy models, Pydantic request/response models
  tests/                  pytest (unit tests; opt-in tests against a real Postgres)
alembic/versions/         every schema change is a migration
evals/                    accuracy eval suite (see above)
docker/                   Postgres 17 + pgvector image for local development and evals
```

## API

All routes are under `/api/v1` and require a Clerk JWT.

| Method and path | Purpose |
|---|---|
| `POST /users/sync` | Create or update the signed-in user (identity from the token) |
| `GET /connections/` · `POST /connections/` · `DELETE /connections/{id}` | List, create (after a live connection test), delete |
| `POST /connections/test` | Test a connection URL without saving it |
| `POST /connections/{id}/index` | Index the schema (SSE: `status`, `progress`, `done`, `error`) |
| `POST /query/` | Ask a question (SSE: `status`, `sql_chunk`, `results`, `done`, `error`) |
| `GET /query/history` | Past questions, paginated |
| `POST /design/generate-schema` · `GET /design/history` · `GET /design/usage` | Schema Designer |

Plan limits return `403` with `CONNECTION_LIMIT_REACHED`, `QUERY_LIMIT_REACHED` or `DESIGN_LIMIT_REACHED`.

## Running locally

Needs Python 3.11+, a Postgres database with the `vector` and `pg_trgm` extensions available (Neon has
both; locally, build `docker/pgvector.Dockerfile`), a [Google AI Studio](https://aistudio.google.com)
key and a [Clerk](https://clerk.com) application.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then fill it in (see below)
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql+asyncpg://…` — the app database (also stores vectors) |
| `GOOGLE_API_KEY` | yes | Gemini API key |
| `ENCRYPTION_KEY` | yes | Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `CLERK_ISSUER` | yes | Your Clerk issuer URL |
| `CLERK_JWKS_URL` | yes | `<issuer>/.well-known/jwks.json` |
| `CORS_ORIGINS` | no | JSON list, default `["http://localhost:3000"]` |
| `ENVIRONMENT` | no | `production` (default: host guard on, fails fast on missing settings) or `development` |
| `ALLOW_PRIVATE_DB_HOSTS` | no | Allow private database hosts in production (self-hosting) |

In `development`, connections to `localhost` and private networks are allowed so you can test against
a local database.

### Tests

```bash
pytest -q app/tests
```

Unit tests mock Gemini and the target database. Opt-in tests run against a real Postgres when
`QM_TEST_TARGET_DATABASE_URL` (any database) or `QM_TEST_PAGILA_URL` (the eval Pagila database) is set.

## Known limitations

- Gemini's free tier allows 20 SQL generations a day for the whole app; a real launch needs a paid key.
- The monthly question limit is counted after each answer, so parallel requests can slightly exceed it.
- No rate limiting yet on connection tests and questions.
- The validator's table check is text-based: in a comma join (`FROM a, b`) only the first table is
  checked. It guards against invented tables; the read-only transaction is the safety boundary.

---

Built by [Agrim Gupta](https://agrimdev.vercel.app) · [LinkedIn](https://linkedin.com/in/agrim-gupta08)
