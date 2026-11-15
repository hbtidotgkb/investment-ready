import os
import tempfile

# Isolate every test session's data dir BEFORE the package is imported.
os.environ["CA_DATA_DIR"] = tempfile.mkdtemp(prefix="ca_test_")
os.environ.pop("ANTHROPIC_API_KEY", None)
