import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.server as server
from app.api.deps import get_db_handler
from vcs.db.sqlite import DBHandler


@pytest.fixture
def db_handler():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("src/vcs/db/schema.sql").read_text())
    yield DBHandler(conn=conn)
    conn.close()


@pytest.fixture
def client(db_handler):
    server.app.dependency_overrides[get_db_handler] = lambda: db_handler
    yield TestClient(server.app)
    server.app.dependency_overrides.clear()


def test_ac1_missing_api_key_header_returns_401(client, monkeypatch):
    monkeypatch.setenv("HTTP_API_KEY", "correct-key")

    response = client.get("/v1/sources")

    assert response.status_code == 401


def test_ac2_wrong_api_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("HTTP_API_KEY", "correct-key")

    response = client.get("/v1/sources", headers={"X-API-Key": "wrong-key"})

    assert response.status_code == 401


def test_ac3_correct_api_key_reaches_the_route(client, monkeypatch, config_path):
    monkeypatch.setenv("HTTP_API_KEY", "correct-key")
    config_path.write_text("sources: []\n")

    response = client.get("/v1/sources", headers={"X-API-Key": "correct-key"})

    assert response.status_code == 200


def test_ac4_unconfigured_key_rejects_every_request(client, monkeypatch):
    monkeypatch.delenv("HTTP_API_KEY", raising=False)

    response = client.get("/v1/sources", headers={"X-API-Key": "anything"})

    assert response.status_code == 401
