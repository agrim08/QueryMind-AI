"""Where the eval databases live and where their data comes from.

Everything runs in one local Docker container: the sample databases (the "user" databases
QueryMind is asked about) and a separate app database that stands in for Neon, so an eval
run never reads or writes production data.
"""
from dataclasses import dataclass
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
DATASETS_DIR = EVALS_DIR / "datasets"
REPORTS_DIR = EVALS_DIR / "reports"
CACHE_DIR = EVALS_DIR / ".cache"
DOWNLOAD_DIR = CACHE_DIR / "downloads"
GENERATION_CACHE_FILE = CACHE_DIR / "generations.json"
QUOTA_LEDGER_FILE = CACHE_DIR / "quota.json"
DAILY_LOG_FILE = REPORTS_DIR / "daily.log"

CONTAINER = "qm-evals"
IMAGE = "querymind/postgres-pgvector:17"  # built from backend/docker/pgvector.Dockerfile
HOST_PORT = 55433
APP_DATABASE = "qm_evals_app"

# Gemini 2.5 Flash free tier: 10 requests/minute and 20 requests/day per project, shared with
# the app itself. One question is one request. Evals take 15 a day and leave 5 for using the app.
DEFAULT_INTERVAL_S = 7.0
DAILY_EVAL_BUDGET = 15


def database_url(database: str) -> str:
    """asyncpg URL for a database in the eval container (trust auth, no password)."""
    return f"postgresql+asyncpg://postgres@localhost:{HOST_PORT}/{database}"


@dataclass(frozen=True)
class Source:
    """A sample database: SQL scripts applied in order to an empty database, or a copy of
    another loaded one (`template`), optionally with look-alike tables added (`distractors`)."""

    database: str
    scripts: tuple[str, ...] = ()
    # Chinook's script runs DROP/CREATE DATABASE and \c itself, so it starts in `postgres`.
    script_creates_database: bool = False
    template: str | None = None  # copy this loaded database instead of running scripts
    distractors: bool = False  # add ~180 look-alike tables (evals/distractors.py)
    cases: str | None = None  # questions file (datasets/<cases>.yaml); default: the dataset's name
    in_daily_run: bool = True  # the scheduled run and a bare `evals.run` include it


# Pinned to tags so scores stay comparable between runs. Pagila v4 needs Postgres 18.
SOURCES: dict[str, Source] = {
    "chinook": Source(
        database="chinook",
        scripts=(
            "https://raw.githubusercontent.com/lerocha/chinook-database/v1.4.5/"
            "ChinookDatabase/DataSources/Chinook_PostgreSql.sql",
        ),
        script_creates_database=True,
    ),
    "pagila": Source(
        database="pagila",
        scripts=(
            "https://raw.githubusercontent.com/devrimgunduz/pagila/pagila-v3.1.0/pagila-schema.sql",
            "https://raw.githubusercontent.com/devrimgunduz/pagila/pagila-v3.1.0/pagila-data.sql",
        ),
    ),
    # Large-schema variants (Phase 4.2): the same data and questions among ~180 decoy tables,
    # beyond the size that is sent to the model whole, so retrieval has to find the tables.
    # Run by name: they'd take the daily budget from the base datasets.
    "chinook_xl": Source(
        database="chinook_xl", template="chinook", distractors=True, cases="chinook", in_daily_run=False
    ),
    "pagila_xl": Source(
        database="pagila_xl", template="pagila", distractors=True, cases="pagila", in_daily_run=False
    ),
}

DAILY_DATASETS = sorted(name for name, source in SOURCES.items() if source.in_daily_run)


def cases_file(dataset: str) -> str:
    """The questions file a dataset uses (a large variant shares its base dataset's)."""
    return SOURCES[dataset].cases or dataset
