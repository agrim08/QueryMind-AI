"""Test-wide setup. Runs before any test module imports the app."""
import os

# Tests raise errors on purpose; they must never reach the real Sentry project. An environment
# variable beats backend/.env, so an empty DSN keeps app.main from starting Sentry.
os.environ["SENTRY_DSN"] = ""
