import os
import tempfile

import pytest

# Must be set before the app is imported anywhere.
os.environ.setdefault("PARTSBIN_DATA_DIR", tempfile.mkdtemp(prefix="partsbin-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c
