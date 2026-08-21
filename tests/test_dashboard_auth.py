from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bot.storage.db import init_db
from bot import web
from bot.web import DashboardHandler


@pytest.fixture()
def server(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    web._reset_dashboard_runtime_state()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def _get_status(port: int, path: str, headers: dict | None = None, method: str = "GET") -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_api_is_public_without_configured_token(server, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    assert _get_status(server.server_port, "/api/status") == 200
    assert _get_status(server.server_port, "/api/analytics") == 200
    assert _get_status(server.server_port, "/api/strategies") == 200
    assert _get_status(server.server_port, "/api/learning") == 200


def test_api_ignores_obsolete_dashboard_token(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    assert _get_status(server.server_port, "/api/status") == 200
    assert _get_status(server.server_port, "/api/status", {"Authorization": "Bearer wrong"}) == 200


def test_force_settlement_requires_admin_token_and_is_rate_limited(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(web, "force_settlements_payload", lambda: {"settlement": {"settled_now": 0}})
    assert _get_status(server.server_port, "/api/settlements/force", method="POST") == 401
    headers = {"Authorization": "Bearer admin-secret"}
    assert _get_status(server.server_port, "/api/settlements/force", headers, method="POST") == 200
    assert _get_status(server.server_port, "/api/settlements/force", headers, method="POST") == 429


def test_force_settlement_rejects_concurrent_request(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    started = threading.Event()
    release = threading.Event()

    def slow_settlement():
        started.set()
        release.wait(timeout=2)
        return {"settlement": {"settled_now": 0}}

    monkeypatch.setattr(web, "force_settlements_payload", slow_settlement)
    first = threading.Thread(
        target=_get_status,
        args=(server.server_port, "/api/settlements/force"),
        kwargs={"headers": {"Authorization": "Bearer admin-secret"}, "method": "POST"},
    )
    first.start()
    assert started.wait(timeout=1)
    assert _get_status(
        server.server_port,
        "/api/settlements/force",
        {"Authorization": "Bearer admin-secret"},
        method="POST",
    ) == 409
    release.set()
    first.join(timeout=2)


def test_status_payload_is_cached(monkeypatch):
    web._reset_dashboard_runtime_state()
    calls = 0

    def payload():
        nonlocal calls
        calls += 1
        return {"generated_at": str(time.monotonic())}

    monkeypatch.setattr(web, "_build_status_payload", payload)
    first = web.status_payload()
    second = web.status_payload()
    assert first == second
    assert calls == 1


def test_analytics_payload_is_cached_and_single_flight(monkeypatch):
    web._reset_dashboard_runtime_state()
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def payload():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return {"generated_at": str(time.monotonic())}

    monkeypatch.setattr(web, "_build_analytics_payload", payload)
    results = []
    ready = threading.Barrier(9)

    def worker() -> None:
        ready.wait()
        results.append(web.analytics_payload())

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for worker in workers:
        worker.start()
    ready.wait()
    assert started.wait(timeout=1)
    release.set()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert len(results) == 8
    assert all(result == results[0] for result in results)
    assert calls == 1


def test_static_frontend_stays_public(server, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    assert _get_status(server.server_port, "/") == 200


def test_health_endpoint_is_public_and_reports_deploy_commit(server, monkeypatch):
    monkeypatch.setenv("DEPLOY_COMMIT", "abc123")
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/healthz")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = __import__("json").load(response)

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["mode"] == "paper"
    assert payload["deploy_commit"] == "abc123"
