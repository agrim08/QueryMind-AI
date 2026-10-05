# Accuracy evals

Real questions against real sample databases, run through the same pipeline as the app
(retrieve → generate → validate → execute), scored by **execution accuracy**: the generated
query passes when it returns the same rows as a hand-written gold query.

Run them before and after any change to prompts, retrieval, indexing or the model, and
record the score below. They call the real Gemini API, so they never run in CI.

## Living within the free tier

Gemini 2.5 Flash's free tier allows **20 generation requests per day per project**, shared with
the app, and resets at midnight US Pacific time (12:30 / 13:30 IST). Three things make that enough:

- **Daily budget:** evals spend at most 15 calls per quota day (`DAILY_EVAL_BUDGET`), tracked in
  `.cache/quota.json` across manual and scheduled runs, so about 5 stay free for using the app.
- **Answer cache:** every Gemini answer is saved under a hash of the exact request (model, prompt,
  tables, config) in `.cache/generations.json`. Re-runs reuse it, so only questions whose prompt
  changed cost quota. Validator, executor and scoring changes re-score all 68 questions for free.
- **Core set first:** 15 questions marked `core: true` (every question type) run before the rest,
  so a prompt change is measured the next day; the full set completes over about five days.

Questions that don't fit today's budget are reported as **pending** and picked up by the next run.

## Setup (once)

Needs Docker and the `querymind/postgres-pgvector:17` image
(`docker build -t querymind/postgres-pgvector:17 -f docker/pgvector.Dockerfile docker`).

```bash
cd backend
python -m evals.setup
```

This starts the `qm-evals` container on port 55433, loads the sample databases (pinned
releases, see `config.py`), and creates a separate app database with the Alembic migrations.
The runner overrides `DATABASE_URL` to point at that database, so evals never touch Neon.
`GOOGLE_API_KEY` and `ENCRYPTION_KEY` are read from `backend/.env`.

### Daily automatic run

```powershell
powershell -ExecutionPolicy Bypass -File backend\evals\schedule.ps1
```

Registers the Windows task "QueryMind daily eval": 1:30 AM daily, or as soon as possible after
that if the PC was off. It starts Docker Desktop and the container if needed, spends the day's
budget and writes `reports/latest.md` (log: `reports/daily.log`). Turn on "Start Docker Desktop
when you sign in" in Docker Desktop's settings. Remove the task with
`Unregister-ScheduledTask -TaskName "QueryMind daily eval" -Confirm:$false`.

## Running by hand

```bash
python -m evals.run                         # all datasets, within today's budget
python -m evals.run --core                  # only the 15 core questions
python -m evals.run chinook --check-gold    # gold queries only, no Gemini calls
python -m evals.run pagila --ids p03,p16    # selected cases
python -m evals.run --budget 3              # at most 3 new Gemini calls
python -m evals.run pagila --reindex        # rebuild the schema index first
python -m evals.run --knowledge             # with each dataset's business definitions
python -m evals.run --core --thinking 0     # Gemini thinking off (compare with the default 1024)
python -m evals.run --until-quota           # catch-up run: spend the whole quota left today
python -m evals.run chinook_xl pagila_xl    # the large-schema variants (not in the daily run)
```

`--until-quota` ignores the 15-call eval budget and calls Gemini until it refuses, so it also uses
the app's share of the day: use it only when you won't need the app until the quota resets.
A refusal (HTTP 429, shown in the app as "QueryMind is busy") is never scored: the question stays
pending for the next run.

Each run writes `reports/<timestamp>.json` (every generated query) and `reports/latest.md`
(the readable summary). Both are git-ignored. Calls are spaced 7 seconds apart for the
per-minute limit, and a run stops calling Gemini after two refusals in a row.

## Retrieval eval

`python -m evals.retrieval [--reindex]` checks, for every question, whether all the tables the gold
SQL reads are shown to the model. The base datasets force the search path (the app sends them
whole); the `_xl` variants are large enough that the app searches. Question embeddings are cached
(`.cache/embeddings.json`), so comparing retrieval changes costs no API calls.

Phase 4.2 results (2026-10-06), share of questions with every needed table shown:

| Strategy | chinook | chinook_xl | pagila | pagila_xl | Avg schema chars |
|---|---|---|---|---|---|
| top 6 + FK links (Phase 1.4), decoys not marked | 94% | 73% | 87% | 68% | ~2,400 |
| top 6 + FK links, empty tables ranked lower | 94% | 91% | 87% | 87% | ~2,500 |
| **top 12 + FK links (the app since 4.2)** | **100%** | **97%** | **100%** | **100%** | ~4,000 |
| top 12 + bridge tables + 2 FK hops | 100% | 97% | 100% | 100% | ~4,600 |

Empty tables (detected at indexing) are ranked lower: an empty table can't answer anything. The
decoys here are all empty, so that step flatters the `_xl` scores; with non-empty look-alikes
(analytics marts) business definitions are what point at the right table. The remaining miss
(c35) needs a three-table chain.

## Business definitions

`datasets/<name>.knowledge.yaml` holds a few short, true definitions per dataset (like BIRD's
evidence sentences). `--knowledge` loads them into the eval connection before the run; without it
they're cleared, so the default run stays the baseline. Reports say which mode they used.

## Scoring

- **Pass:** `exact` (same rows), `extra_columns` (every gold column present, rows match),
  `declined` on a question the database can't answer, or `asked` (a clarifying question) on a
  case tagged `ambiguous`.
- **Fail:** `mismatch`, `invalid_sql` (rejected by the validator), `error` (generation or
  execution failed), `wrongly_declined`, `should_decline`, `asked_unnecessarily` (a clarifying
  question on a clear question), `answered_in_words` (a text answer where rows were needed).
- Row order is ignored. Numbers are compared to 2 decimals. A case may list several gold
  queries when the question genuinely has more than one reading; matching any one passes.

## Writing cases

See the header of `datasets/chinook.yaml`. Phrase questions the way a non-technical user
would. Gold SQL selects only what the question asks for. Avoid questions whose answer
depends on ties at a `LIMIT` cut-off, and run `--check-gold` after editing.

## Datasets

| Dataset | Domain | Size | Notes |
|---|---|---|---|
| chinook | music store | 11 tables | small, clean names |
| pagila | DVD rental | 22 tables (7 partitions) + 7 views | UPPER CASE data, partitions, enum and array columns |
| chinook_xl | chinook + 180 look-alike tables | 191 relations | large-schema variant (Phase 4.2), same questions |
| pagila_xl | pagila + 180 look-alike tables | 203 relations | large-schema variant (Phase 4.2), same questions |

The `_xl` variants are copies of the base databases plus ~180 empty tables in 12 schemas
(`evals/distractors.py`): CRM, finance, HR, analytics marts, legacy archives… many deliberately
named like the real ones (`crm.customer`, `finance.invoice`, `legacy.payment_archive`). They're
beyond the size the app sends whole, so retrieval has to pick the right tables among decoys.
The decoys are empty, which the indexer detects; real company databases would also have
non-empty look-alikes (analytics marts), where business definitions decide which table is meant.


## Results

| Date | Change | chinook | pagila | Median first SQL token |
|---|---|---|---|---|
| 2026-10-04 | Baseline (partial: Gemini free tier allows 20 requests/day) | 8/11 (c01–c11) | not run | 2.2 s |
| 2026-10-04 | Validator ignores `FROM` inside `EXTRACT(…)` and similar (re-scored from cache) | 9/11 (c01–c11) | not run | — |
| 2026-10-05 | Core set; prompt: rolling time windows, LIMIT rule (1 Gemini refusal left pending) | 7/8 core | 4/6 core | 2.8 s |

Baseline failures: c05 typo in a name (`ILIKE '%zepelin%'`), c08 valid `EXTRACT(YEAR FROM …)`
rejected by the validator's table check (fixed), c09 "top 5" answered with `LIMIT 500`.
