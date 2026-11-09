from functools import wraps

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def loopback_testclient_default(monkeypatch):
    """Use a real allowed host; explicit hostile-host security tests keep their URL."""
    original = TestClient.__init__

    @wraps(original)
    def initialize(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", initialize)
