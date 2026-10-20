import importlib.util

import pytest


def test_direct_policy_defaults_to_deny(monkeypatch):
    monkeypatch.delenv("IACPAAS_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("IACPAAS_WRITABLE_PREFIX", raising=False)
    assert importlib.util.find_spec("iacpaas_mcp.policy") is not None
    from iacpaas_mcp.policy import WritePolicy

    with pytest.raises(PermissionError):
        WritePolicy.from_env().authorize("/demo/university")


def test_env_opt_in_only_allows_canonical_mock_prefix(monkeypatch):
    from iacpaas_mcp.policy import WritePolicy

    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "1")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    policy = WritePolicy.from_env()
    policy.authorize("/demo/university")
    for path in ["/other/x", "/demolition/x", "/demo/../private", "/demo/%2e%2e/x", "/demo//x"]:
        with pytest.raises(PermissionError):
            policy.authorize(path)
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/")
    with pytest.raises(PermissionError):
        WritePolicy.from_env().authorize("/demo/university")
    monkeypatch.setenv("IACPAAS_WRITABLE_PREFIX", "/demo/")
    monkeypatch.setenv("IACPAAS_ALLOW_WRITES", "true")
    with pytest.raises(PermissionError):
        WritePolicy.from_env().authorize("/demo/university")
