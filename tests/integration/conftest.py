import pytest
from fastapi.testclient import TestClient
from helpers import BASE, ORIGIN, PORT

from kovan.config import AppConfig
from kovan.web.app import create_app


@pytest.fixture
def client(tmp_path, code_root):
    app = create_app(AppConfig(data_dir=tmp_path / "data", port=PORT, poll_interval=0.5))
    app.state.ctx.repo.set_setting("roots", [str(code_root)])
    with TestClient(app, base_url=BASE) as c:
        c.ctx = app.state.ctx
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/").text
    csrf = page.split('name="csrf-token" content="')[1].split('"')[0]
    client.headers.update({"X-CSRF-Token": csrf, **ORIGIN})
    return client
