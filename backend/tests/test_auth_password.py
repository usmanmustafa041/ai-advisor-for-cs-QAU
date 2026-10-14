import pytest
from fastapi import HTTPException

from app.api.auth import change_password
from app.core.auth import hash_password, verify_password
from app.schemas.auth import ChangePasswordRequest


class _PasswordResult:
    def __init__(self, password_hash):
        self.password_hash = password_hash

    def mappings(self):
        return self

    def one_or_none(self):
        return {"password_hash": self.password_hash}


class _PasswordDb:
    def __init__(self, password_hash):
        self.password_hash = password_hash
        self.calls = []
        self.committed = False

    def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, params))
        if sql.startswith("SELECT password_hash"):
            return _PasswordResult(self.password_hash)
        return None

    def commit(self):
        self.committed = True


def test_change_password_updates_hash_and_revokes_sessions():
    db = _PasswordDb(hash_password("old-password"))
    payload = ChangePasswordRequest(
        current_password="old-password",
        new_password="new-password-123",
        confirm_password="new-password-123",
    )

    result = change_password(payload, {"id": "user-1"}, db)

    assert result["password_changed"] is True
    password_update = next(params for sql, params in db.calls if sql.startswith("UPDATE app_users"))
    assert verify_password("new-password-123", password_update["password"])
    assert any(sql.startswith("UPDATE auth_sessions") for sql, _ in db.calls)
    assert db.committed is True


def test_change_password_rejects_wrong_current_password():
    db = _PasswordDb(hash_password("old-password"))
    payload = ChangePasswordRequest(
        current_password="wrong-password",
        new_password="new-password-123",
        confirm_password="new-password-123",
    )

    with pytest.raises(HTTPException) as error:
        change_password(payload, {"id": "user-1"}, db)

    assert error.value.status_code == 400
    assert error.value.detail == "Current password is incorrect"
    assert db.committed is False
