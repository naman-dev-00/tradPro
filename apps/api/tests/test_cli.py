import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base
from src.models import User, LEGACY_PRINCIPAL_ID
from src.cli import (
    cmd_create_admin,
    cmd_create_user,
    cmd_list_users,
    cmd_reset_password,
    cmd_set_status,
    cmd_cleanup_sessions,
)

class MockArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

def test_cli_create_admin_and_user(session, monkeypatch):
    # Mock getpass to return matching password
    monkeypatch.setattr("getpass.getpass", lambda prompt: "SecurePass1234!")
    monkeypatch.setattr("src.cli.get_db", lambda: session)

    # 1. Create Admin
    args_admin = MockArgs(username="cli_admin", email="admin@cli.example.com")
    cmd_create_admin(args_admin)

    admin_u = session.query(User).filter(User.normalized_username == "cli_admin").first()
    assert admin_u is not None
    assert admin_u.role == "ADMIN"
    assert admin_u.is_active is True

    # 2. Create User
    args_user = MockArgs(username="cli_editor", email="editor@cli.example.com", role="EDITOR")
    cmd_create_user(args_user)

    editor_u = session.query(User).filter(User.normalized_username == "cli_editor").first()
    assert editor_u is not None
    assert editor_u.role == "EDITOR"

    # 3. Reset Password
    monkeypatch.setattr("getpass.getpass", lambda prompt: "NewSecurePass5678!")
    args_reset = MockArgs(username="cli_editor")
    cmd_reset_password(args_reset)
    editor_u = session.query(User).filter(User.normalized_username == "cli_editor").first()
    assert editor_u.hashed_password.startswith("$argon2id$")

    # 4. Set Status
    args_status = MockArgs(username="cli_editor", active="false")
    cmd_set_status(args_status)
    editor_u = session.query(User).filter(User.normalized_username == "cli_editor").first()
    assert editor_u.is_active is False

    # 5. Session cleanup
    args_cleanup = MockArgs(retention_days=7)
    cmd_cleanup_sessions(args_cleanup)
