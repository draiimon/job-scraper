import json
from pathlib import Path

import pytest

from src.config import Settings
from src.jobstreet_link import (
    complete_request,
    connection_status,
    create_request,
    disconnect,
    request_for_token,
    restore_latest_session,
    save_verified_session,
    windows_connector_ps1,
    windows_connector_python,
)
from src.services import Repository


def configured(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/links.db",
        app_secret_key="test-app-secret",
        public_base_url="https://agent.example",
    )


def test_signed_one_time_request_is_bound_and_cannot_replay(tmp_path):
    cfg = configured(tmp_path)
    repo = Repository(cfg.database_url)
    repo.create_schema()
    token = create_request(repo, 123, app_secret_key=cfg.app_secret_key)
    request = request_for_token(repo, token, app_secret_key=cfg.app_secret_key)
    assert request and request.discord_user_id == "123"
    assert token not in request.token_digest
    assert request_for_token(repo, token, consume=True, app_secret_key=cfg.app_secret_key)
    assert request_for_token(repo, token, app_secret_key=cfg.app_secret_key) is None
    assert request_for_token(repo, "wrong-token", app_secret_key=cfg.app_secret_key) is None


def test_session_is_encrypted_at_rest_and_restores_only_to_private_file(tmp_path):
    cfg = configured(tmp_path)
    repo = Repository(cfg.database_url)
    repo.create_schema()
    state = {"cookies": [{"name": "session", "value": "private"}], "origins": []}
    save_verified_session(cfg, repo, 123, state)
    assert connection_status(cfg, repo, 123) == "READY"
    with repo.sessions() as session:
        stored = session.execute(
            __import__("sqlalchemy").select(
                __import__("src.models", fromlist=["SourceConnection"]).SourceConnection
            )
        ).scalar_one()
    assert "private" not in stored.encrypted_session
    path = Path(tmp_path / "private" / "runtime.json")
    assert restore_latest_session(cfg, repo, path)
    assert json.loads(path.read_text()) == state
    assert disconnect(repo, 123)
    assert connection_status(cfg, repo, 123) == "AUTH REQUIRED"


def test_connector_upload_consumes_token_and_sets_ready(tmp_path):
    cfg = configured(tmp_path)
    repo = Repository(cfg.database_url)
    repo.create_schema()
    token = create_request(repo, 456, app_secret_key=cfg.app_secret_key)
    state = {"cookies": [{"name": "session", "value": "uploaded"}], "origins": []}
    assert complete_request(cfg, repo, token, state) == "READY"
    assert connection_status(cfg, repo, 456) == "READY"
    with pytest.raises(Exception, match="expired or already used"):
        complete_request(cfg, repo, token, state)


def test_windows_connector_is_local_playwright_and_has_no_browserless_dependency(tmp_path):
    cfg = configured(tmp_path)
    script = windows_connector_ps1(cfg.public_base_url, "signed-token", cfg)
    helper = windows_connector_python()
    assert "Browserless" not in script
    assert "Browserless" not in helper
    assert "chromium.launch(headless=False)" in helper
    assert "X-JobStreet-Connection-Token" in helper
    assert "connector.py" in script
    assert "signed-token" in script