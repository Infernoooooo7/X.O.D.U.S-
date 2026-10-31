"""Per-install API token (security table: cross-origin and local-network attacks).

Property tested: requests without the token, with a wrong one, from another origin or with a foreign Host are refused;
the UI gets the token only as an HttpOnly, SameSite=Strict cookie on its own pages."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import ScriptedProvider, api_client
from xodus.api.app import create_app
from xodus.security.token import FILENAME, HEADER, cookie_name, load_or_create


def app_for(make_settings):
    return create_app(make_settings(), provider=ScriptedProvider())


def test_every_api_route_needs_the_token(make_settings):
    app = app_for(make_settings)
    with TestClient(app, base_url="http://127.0.0.1") as anonymous:
        for method, path, body in [("GET", "/api/health", None), ("GET", "/api/resources", None),
                                   ("GET", "/api/audit", None), ("POST", "/api/chat", {"message": "hi"}),
                                   ("POST", "/api/tasks", {"goal": "x"}), ("POST", "/api/ingestion/pause", {}),
                                   ("GET", "/api/does-not-exist", None)]:
            r = anonymous.request(method, path, json=body)
            assert r.status_code == 401, path
            assert r.json()["error"]["code"] == "unauthorized" and HEADER in r.json()["error"]["message"]
        wrong = anonymous.get("/api/health", headers={HEADER: "x" * 43})
        assert wrong.status_code == 401
        short = anonymous.get("/api/health", headers={HEADER: app.state.api_token[:-1]})
        assert short.status_code == 401
        nothing_ran = anonymous.get("/api/health", headers={HEADER: app.state.api_token}).json()
        assert nothing_ran["envelope"]["running"] == 0


def test_the_ui_gets_it_as_a_strict_httponly_cookie_and_the_cookie_works(make_settings):
    app = app_for(make_settings)
    with TestClient(app, base_url="http://127.0.0.1") as browser:
        page = browser.get("/")
        assert page.status_code == 200
        header = page.headers["set-cookie"]
        assert header.startswith(f"{cookie_name(8765)}={app.state.api_token}")
        assert "HttpOnly" in header and "SameSite=strict" in header and "Path=/" in header
        assert app.state.api_token not in page.text  # never written into the page where scripts could read it
        assert browser.get("/api/health").status_code == 200  # the cookie alone is enough
        assert browser.get("/api/health", cookies={cookie_name(8765): "wrong"}).status_code == 401


def test_a_valid_token_does_not_bypass_origin_or_host_checks(make_settings):
    app = app_for(make_settings)
    with api_client(app) as client:
        r = client.post("/api/ingestion/pause", json={}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = client.post("/api/ingestion/pause", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        r = client.get("/api/health", headers={"Host": "attacker.example:8765"})  # DNS rebinding
        assert r.status_code == 400


def test_the_token_is_generated_once_per_install_and_survives_restarts(make_settings, tmp_path):
    first = app_for(make_settings).state.api_token
    second = app_for(make_settings).state.api_token
    assert first == second and len(first) >= 40
    assert (tmp_path / "data" / FILENAME).read_text(encoding="ascii").strip() == first
    other = tmp_path / "other"
    other.mkdir()
    assert load_or_create(other) != first  # another install, another token
    (tmp_path / "data" / FILENAME).write_text("short", encoding="ascii")  # damaged: replaced, never accepted
    assert len(app_for(make_settings).state.api_token) >= 40
