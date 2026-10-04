"""Prepare the eval environment: python -m evals.setup (run from backend/).

1. Starts the `qm-evals` Postgres container (creating it if needed).
2. Loads each sample database that isn't there yet (existing databases are left alone).
3. Creates the eval app database and runs the Alembic migrations on it.

Safe to re-run: every step skips work that is already done.
"""
import logging
import os
import subprocess
import sys
import time
import urllib.request

from evals.config import (
    APP_DATABASE,
    CONTAINER,
    DOWNLOAD_DIR,
    HOST_PORT,
    IMAGE,
    SOURCES,
    Source,
    database_url,
)

logger = logging.getLogger("evals.setup")

# The scheduled run has no console; without this every docker call would flash a window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _docker(*args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        check=False,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode(errors="replace").strip()


def _psql(database: str, sql: str) -> str:
    return _docker("exec", CONTAINER, "psql", "-U", "postgres", "-d", database, "-tAc", sql)


def start_container() -> None:
    try:
        running = _docker("inspect", "-f", "{{.State.Running}}", CONTAINER) == "true"
    except RuntimeError:
        logger.info("Creating container %s on port %d", CONTAINER, HOST_PORT)
        _docker("run", "-d", "--name", CONTAINER, "-p", f"{HOST_PORT}:5432", IMAGE)
        running = True
    if not running:
        _docker("start", CONTAINER)

    for _ in range(30):
        try:
            _docker("exec", CONTAINER, "pg_isready", "-U", "postgres", "-q")
            return
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError(f"Postgres in {CONTAINER} did not become ready")


def _database_exists(name: str) -> bool:
    return _psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'") == "1"


def _download(url: str) -> bytes:
    """Fetch a script on the host (container TLS is unreliable) and cache it."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOAD_DIR / url.rsplit("/", 1)[-1]
    if not path.exists():
        logger.info("Downloading %s", url)
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
    return path.read_bytes()


def load_dataset(source: Source) -> None:
    if _database_exists(source.database):
        logger.info("Database %s already loaded", source.database)
        return
    if not source.script_creates_database:
        _psql("postgres", f"CREATE DATABASE {source.database}")
    target = "postgres" if source.script_creates_database else source.database
    for url in source.scripts:
        logger.info("Loading %s into %s", url.rsplit("/", 1)[-1], source.database)
        _docker(
            "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", target, "-q", "-v", "ON_ERROR_STOP=1",
            stdin=_download(url),
        )


def prepare_app_database() -> None:
    if not _database_exists(APP_DATABASE):
        _psql("postgres", f"CREATE DATABASE {APP_DATABASE}")
    logger.info("Migrating %s", APP_DATABASE)
    env = {**os.environ, "DATABASE_URL": database_url(APP_DATABASE)}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start_container()
    for source in SOURCES.values():
        load_dataset(source)
    prepare_app_database()
    logger.info("Eval environment ready. Run: python -m evals.run <%s>", "|".join(SOURCES))


if __name__ == "__main__":
    main()
