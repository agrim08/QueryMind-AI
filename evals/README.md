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
```

Each run writes `reports/<timestamp>.json` (every generated query) and `reports/latest.md`
(the readable summary). Both are git-ignored. Calls are spaced 7 seconds apart for the
per-minute limit, and a run stops calling Gemini after two refusals in a row.

## Retrieval eval

`python -m evals.retrieval` forces the large-schema search path (the eval databases are small enough
to be sent whole in the app) and checks whether every table the gold SQL reads is shown to the model.
Embedding calls only. Results on 2026-10-04 (top 6 tables):

| Dataset | Vector only | Vector + FK links | Hybrid + FK links |
|---|---|---|---|
| chinook | 31/33 (94%) | 31/33 (94%) | 31/33 (94%) |
| pagila | 21/31 (68%) | 27/31 (87%) | 27/31 (87%) |

Foreign-key expansion is the big win. Hybrid search ties on whole questions but misses fewer tables
inside the failing ones. The remaining misses are 4–5-table join chains (film → inventory → rental →
payment); two-hop FK expansion is the likely next step.

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

AdventureWorks (~70 tables across several schemas) is added with Phase 1.3, once the
indexer reads schemas other than `public`.

## Results

| Date | Change | chinook | pagila | Median first SQL token |
|---|---|---|---|---|
| 2026-10-04 | Baseline (partial: Gemini free tier allows 20 requests/day) | 8/11 (c01–c11) | not run | 2.2 s |
| 2026-10-04 | Validator ignores `FROM` inside `EXTRACT(…)` and similar (re-scored from cache) | 9/11 (c01–c11) | not run | — |

Baseline failures: c05 typo in a name (`ILIKE '%zepelin%'`), c08 valid `EXTRACT(YEAR FROM …)`
rejected by the validator's table check (fixed), c09 "top 5" answered with `LIMIT 500`.
