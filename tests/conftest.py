import os
import tempfile

import pytest

# Isolate every test session's data dir BEFORE the package is imported.
os.environ["CA_DATA_DIR"] = tempfile.mkdtemp(prefix="ca_test_")
os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.fixture(autouse=True)
def no_real_kite_session(monkeypatch):
    """Tests never touch the user's real Kite MCP login (it lives in the OS keychain)."""
    from ca_platform.data import kite_mcp
    monkeypatch.setattr(kite_mcp, "_load_token", lambda: None)
