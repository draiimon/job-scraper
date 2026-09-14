import json
from pathlib import Path

from cryptography.fernet import Fernet

from src.config import Settings
from src.jobstreet_link import (
    connection_status, create_request, disconnect, request_for_token,
    restore_latest_session, save_verified_session,
)
from src.services import Repository


def configured(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/links.db",
        jobstreet_session_encryption_key=Fernet.generate_key().decode(),
    )


def test_one_time_request_is_opaque_bound_and_cannot_replay(tmp_path):
    cfg = configured(tmp_path); repo = Repository(cfg.database_url); repo.create_schema()
    token = create_request(repo, 123)
    request = request_for_token(repo, token)
    assert request and request.discord_user_id == "123" and token not in request.token_digest
    assert request_for_token(repo, token, consume=True)
    assert request_for_token(repo, token) is None
    assert request_for_token(repo, "wrong-token") is None


def test_session_is_encrypted_at_rest_and_restores_only_to_private_file(tmp_path):
    cfg = configured(tmp_path); repo = Repository(cfg.database_url); repo.create_schema()
    state = {"cookies": [{"name": "session", "value": "private"}], "origins": []}
    save_verified_session(cfg, repo, 123, state)
    assert connection_status(cfg, repo, 123) == "READY"
    with repo.sessions() as session:
        stored = session.execute(__import__('sqlalchemy').select(__import__('src.models', fromlist=['SourceConnection']).SourceConnection)).scalar_one()
    assert "private" not in stored.encrypted_session
    path = Path(tmp_path / "private" / "runtime.json")
    assert restore_latest_session(cfg, repo, path)
    assert json.loads(path.read_text()) == state
    assert disconnect(repo, 123)
    assert connection_status(cfg, repo, 123) == "AUTH REQUIRED"


def test_existing_application_secret_derives_session_encryption_without_a_second_secret(tmp_path):
    cfg = Settings(database_url=f"sqlite:///{tmp_path}/derived.db", app_secret_key="private-app-secret")
    repo = Repository(cfg.database_url); repo.create_schema()
    state = {"cookies": [], "origins": []}
    save_verified_session(cfg, repo, 123, state)
    assert restore_latest_session(cfg, repo, tmp_path / "runtime.json")
