from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


COMPUTE_PATH = Path(__file__).resolve().parents[1] / "rockie-compute"


def _load_compute(name: str = "rockie_compute"):
    loader = importlib.machinery.SourceFileLoader(name, str(COMPUTE_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_build_request_sends_auth_token_and_tenant_id(monkeypatch):
    monkeypatch.setenv("ROCKIELAB_TENANT_TOKEN", "service-token")
    monkeypatch.setenv("ROCKIELAB_TENANT_ID", "t-compute")
    compute = _load_compute()

    req = compute._build_request("GET", "/api/compute/target", None)
    headers = dict(req.header_items())

    assert headers["X-tenant-token"] == "service-token"
    assert headers["X-tenant-id"] == "t-compute"
    assert headers["User-agent"].startswith("rockie-runtime/")
    assert "Python-urllib" not in headers["User-agent"]


def test_build_request_uses_tenant_dev_token_alias(monkeypatch):
    monkeypatch.delenv("ROCKIELAB_TENANT_TOKEN", raising=False)
    monkeypatch.setenv("ROCKIELAB_TENANT_DEV_TOKEN", "dev-service-token")
    monkeypatch.setenv("ROCKIELAB_TENANT_ID", "t-compute")
    compute = _load_compute("rockie_compute_dev_token_alias")

    req = compute._build_request("GET", "/api/compute/target", None)
    headers = dict(req.header_items())

    assert headers["X-tenant-token"] == "dev-service-token"
    assert headers["X-tenant-id"] == "t-compute"


def test_build_request_uses_pat_bearer_without_tenant_id(monkeypatch):
    monkeypatch.setenv("ROCKIELAB_TENANT_TOKEN", "rl_pat_testtoken")
    monkeypatch.delenv("ROCKIELAB_TENANT_ID", raising=False)
    compute = _load_compute("rockie_compute_pat_auth")

    req = compute._build_request("GET", "/api/jobs", None)
    headers = dict(req.header_items())

    assert headers["Authorization"] == "Bearer rl_pat_testtoken"
    assert "X-tenant-token" not in headers
    assert "X-tenant-id" not in headers


def test_service_token_still_requires_tenant_id(monkeypatch):
    monkeypatch.setenv("ROCKIELAB_TENANT_TOKEN", "service-token")
    monkeypatch.delenv("ROCKIELAB_TENANT_ID", raising=False)
    compute = _load_compute("rockie_compute_missing_tid")

    try:
        compute._build_request("GET", "/api/jobs", None)
    except compute.CLIError as exc:
        assert exc.exit_code == 2
        assert "ROCKIELAB_TENANT_ID" in str(exc)
    else:
        raise AssertionError("service-token auth must require ROCKIELAB_TENANT_ID")


def test_legacy_status_and_cancel_keep_compute_endpoints(monkeypatch):
    monkeypatch.setenv("ROCKIELAB_TENANT_TOKEN", "rl_pat_testtoken")
    compute = _load_compute("rockie_compute_legacy_endpoints")
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return None
        return {"handle": "job-1", "state": "RUNNING"}

    monkeypatch.setattr(compute, "_http", fake_http)
    monkeypatch.setattr(compute, "_emit", lambda obj, human=False: None)

    parser = compute.build_parser()
    assert parser.parse_args(["status", "job-1"]).func(
        parser.parse_args(["status", "job-1"])
    ) == 0
    assert parser.parse_args(["cancel", "job-1"]).func(
        parser.parse_args(["cancel", "job-1"])
    ) == 0

    assert calls == [
        ("GET", "/api/compute/jobs/job-1", None),
        ("DELETE", "/api/compute/jobs/job-1", None),
    ]


def test_new_operator_commands_use_unified_jobs_endpoints(monkeypatch):
    monkeypatch.setenv("ROCKIELAB_TENANT_TOKEN", "rl_pat_testtoken")
    compute = _load_compute("rockie_compute_jobs_endpoints")
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/jobs/credit-balance":
            return {"balance_cents": 100}
        if path == "/api/jobs?limit=7":
            return []
        if method == "POST":
            return None
        return {"id": "job-1", "state": "RUNNING"}

    monkeypatch.setattr(compute, "_http", fake_http)
    monkeypatch.setattr(compute, "_emit", lambda obj, human=False: None)

    parser = compute.build_parser()
    assert parser.parse_args(["ls", "--limit", "7"]).func(
        parser.parse_args(["ls", "--limit", "7"])
    ) == 0
    assert parser.parse_args(["detail", "job-1"]).func(
        parser.parse_args(["detail", "job-1"])
    ) == 0
    assert parser.parse_args(["stop", "job-1"]).func(
        parser.parse_args(["stop", "job-1"])
    ) == 0
    assert parser.parse_args(["balance"]).func(parser.parse_args(["balance"])) == 0

    assert calls == [
        ("GET", "/api/jobs?limit=7", None),
        ("GET", "/api/jobs/job-1", None),
        ("POST", "/api/jobs/job-1/cancel", None),
        ("GET", "/api/jobs/credit-balance", None),
    ]


def test_rockie_wrapper_dispatches_to_absolute_compute_binary():
    wrapper = COMPUTE_PATH.parent / "rockie"
    src = wrapper.read_text(encoding="utf-8")

    assert "exec /usr/local/bin/rockie-compute" in src
    assert "exec rockie-compute" not in src
