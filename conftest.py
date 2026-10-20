"""Root conftest -- exposes the shared test fixtures to the whole repo.

The fixtures themselves live in tests/conftest.py. This module re-exports them
so that test modules outside tests/ (e.g. mcp_client/) can use them too,
without duplicating the FakeR2 implementation.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

# Re-export the shared fixtures so pytest registers them repo-wide.
from tests.conftest import (  # noqa: F401,E402
    FakeR2,
    NoSuchKey,
    TEST_BUCKET,
    TEST_SECRET,
    anyio_backend,
    client_factory,
    fake_r2,
    make_token,
    no_network_guard,
    sse_rpc,
    valid_token,
)

# Autouse guard against tests reaching the real R2 bucket. Must be re-exported
# here too, or tests outside tests/ (e.g. mcp_client/) cannot resolve it and
# error at fixture setup.
from tests.conftest import _isolate_r2_by_default  # noqa: F401,E402
