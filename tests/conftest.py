"""Shared fixtures + env setup for the test suite.

Imports of `app`/`auth`/`setup_flow` MUST happen after env vars are set, since
those modules read configuration at import time. Setting AUTH_MODE=none and
pointing CLAUDE_HOME / CLAUDE_WEB_STATE_DIR / CLAUDE_PROJECT_DIR at temp
directories keeps tests isolated from the host's real Claude installation.
"""
from __future__ import annotations

import os
import shutil
import tempfile

# Pre-import setup must run BEFORE any `import app` in the suite. Use a
# module-level block so even bare `pytest --collect-only` sees them.
_TEST_TMP = tempfile.mkdtemp(prefix="claude-web-tests-")
# Force-override (not setdefault) for vars that a developer's local .env may
# have exported into their shell. Tests must run against a hermetic config —
# otherwise OIDC_REDIRECT_URI from a real deployment leaks into expected_origin,
# AUTH_MODE=oidc breaks the anonymous fixture, and CSRF rejects every test POST.
for var in (
    "AUTH_MODE",
    "CLAUDE_HOME",
    "CLAUDE_WEB_STATE_DIR",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_WEB_ENABLE_SETUP",
    "CLAUDE_WEB_CSRF_STRICT",
    "OIDC_REDIRECT_URI",
    "OIDC_ISSUER_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_ALLOWED_EMAILS",
    "OIDC_ALLOWED_GROUPS",
    "SESSION_SECRET",
    "CLAUDE_WEB_PER_USER_SESSIONS",
    "CLAUDE_WEB_PROJECT_DIRS",
    "ANTHROPIC_API_KEY",
):
    os.environ.pop(var, None)
os.environ["AUTH_MODE"] = "none"
os.environ["CLAUDE_HOME"] = os.path.join(_TEST_TMP, "claude-home")
os.environ["CLAUDE_WEB_STATE_DIR"] = os.path.join(_TEST_TMP, "claude-web-state")
os.environ["CLAUDE_PROJECT_DIR"] = os.path.join(_TEST_TMP, "project")
os.environ["CLAUDE_WEB_ENABLE_SETUP"] = "true"
# Loosen CSRF so tests using the bare TestClient don't all need to set Origin.
os.environ["CLAUDE_WEB_CSRF_STRICT"] = "false"
for d in (
    os.environ["CLAUDE_HOME"],
    os.environ["CLAUDE_WEB_STATE_DIR"],
    os.environ["CLAUDE_PROJECT_DIR"],
):
    os.makedirs(d, exist_ok=True)


import pytest


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp() -> None:
    yield
    shutil.rmtree(_TEST_TMP, ignore_errors=True)


@pytest.fixture
def client():
    """A FastAPI TestClient bound to the freshly-imported app.

    Lazy import so each test that pulls this fixture sees the env-configured
    module (rather than a cached version that read env vars before the
    fixture set them).
    """
    from fastapi.testclient import TestClient
    import app as app_module

    return TestClient(app_module.app)
