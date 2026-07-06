from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bot.storage.db import init_db
from bot.web import DashboardHandler


@pytest.fixture()
def server(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
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


def test_api_fails_closed_without_configured_token(server, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    assert _get_status(server.server_port, "/api/status") == 403


def test_api_rejects_missing_or_wrong_token(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    assert _get_status(server.server_port, "/api/status") == 401
    assert _get_status(server.server_port, "/api/status", {"Authorization": "Bearer wrong"}) == 401
    assert _get_status(server.server_port, "/api/settlements/force", method="POST") == 401


def test_api_accepts_valid_token_via_bearer_and_query(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    assert _get_status(server.server_port, "/api/status", {"Authorization": "Bearer secret"}) == 200
    assert _get_status(server.server_port, "/api/status?token=secret") == 200


def test_static_frontend_stays_public(server, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    assert _get_status(server.server_port, "/") == 200
