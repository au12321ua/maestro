"""The web fallback survives odd paths, and every response carries the
browser hardening headers."""

import logging

import pytest
from fastapi.testclient import TestClient
from support import setup_admin

from maestro.api import app as app_module
from maestro.api.app import create_app

HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "content-security-policy": "frame-ancestors 'none'",
}
ODD_PATHS = [
    "/%00",
    "/assets/%00x",
    "/" + "a" * 256,
    "/assets/" + "b" * 4000,
    "/x/" + "c" * 300 + "/y",
]


@pytest.fixture
def web(service, tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>app</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(app_module, "STATIC", static.resolve())
    with TestClient(create_app(service, run_worker=False), raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    "path", ODD_PATHS, ids=["nul", "nul-in-dir", "long", "long-in-dir", "long-mid"]
)
def test_unusual_paths_fall_back_instead_of_failing(web, caplog, path):
    with caplog.at_level(logging.ERROR):
        response = web.get(path)
    assert response.status_code == 200 and response.text == "<html>app</html>"
    assert "Traceback" not in caplog.text and "unhandled error" not in caplog.text


def test_files_and_the_fallback_still_work(web):
    assert web.get("/assets/app.js").text == "console.log(1)"
    assert web.get("/runs/abc").text == "<html>app</html>"
    assert web.get("/../../etc/passwd").text == "<html>app</html>"
    unknown = web.get("/api/v1/nope")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "NOT_FOUND"


def test_every_kind_of_response_carries_the_hardening_headers(web, service, monkeypatch):
    admin = setup_admin(service, web)
    responses = [
        web.get("/"),  # fallback page
        web.get("/assets/app.js"),  # static file
        web.get("/api/v1/capabilities"),  # API success
        web.get("/api/v1/nope"),  # API 404
        TestClient(web.app).get("/api/v1/me"),  # 401
        web.post("/api/v1/me/pats", json={}),  # 403 without the CSRF header
        admin.post("/api/v1/me/pats", json={"name": "x"}),  # 422
        web.get("/api/docs"),  # Swagger page
    ]

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service.accounts, "me", broken)
    crashed = admin.get("/api/v1/me")
    assert crashed.status_code == 500
    for response in [*responses, crashed]:
        for name, value in HEADERS.items():
            assert response.headers.get(name) == value, (response.request.url, name)
