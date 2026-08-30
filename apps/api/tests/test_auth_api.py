import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.database import get_db
from src.models import User, LEGACY_PRINCIPAL_ID
from src.auth.security import hash_password
from src.auth.rate_limiter import rate_limiter
from src.config import settings

def test_production_insecure_cookie_startup_rejection(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    with pytest.raises(RuntimeError) as exc_info:
        settings.verify_security_settings()
    assert "requires secure cookies" in str(exc_info.value)

def test_csrf_bootstrap_and_login_flow(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        # 1. Create active test user
        pwd = "CorrectHorseBattery99!"
        user = User(
            username="trader_dan",
            normalized_username="trader_dan",
            email="dan@example.com",
            normalized_email="dan@example.com",
            hashed_password=hash_password(pwd),
            role="EDITOR",
            is_active=True
        )
        session.add(user)
        session.commit()

        # 2. Bootstrap CSRF token
        csrf_res = client.get("/api/v1/auth/csrf-token")
        assert csrf_res.status_code == 200
        csrf_token = csrf_res.json()["csrf_token"]
        assert "tradepro_csrf_preauth" in client.cookies

        # 3. Test Invalid Origin -> 403
        bad_origin_res = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "trader_dan", "password": pwd},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://evil-attacker.com"}
        )
        assert bad_origin_res.status_code == 403

        # 4. Test Mismatched CSRF -> 403
        bad_csrf_res = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "trader_dan", "password": pwd},
            headers={"X-CSRF-Token": "wrong_csrf_token", "Origin": "http://localhost:3000"}
        )
        assert bad_csrf_res.status_code == 403

        # 5. Test Invalid Credentials -> 401
        bad_pwd_res = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "trader_dan", "password": "WrongPassword123!"},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://localhost:3000"}
        )
        assert bad_pwd_res.status_code == 401

        # 6. Test Legacy Principal Login Blocked -> 401
        legacy_user = User(
            id=LEGACY_PRINCIPAL_ID,
            username="system_legacy_owner",
            normalized_username="system_legacy_owner",
            email="system_legacy@tradepro.internal",
            normalized_email="system_legacy@tradepro.internal",
            hashed_password="!DISABLED",
            role="VIEWER",
            is_active=False
        )
        session.add(legacy_user)
        session.commit()

        legacy_res = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "system_legacy_owner", "password": pwd},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://localhost:3000"}
        )
        assert legacy_res.status_code == 401

        # 7. Successful Login
        login_res = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "trader_dan", "password": pwd},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://localhost:3000"}
        )
        assert login_res.status_code == 200
        user_data = login_res.json()
        assert user_data["username"] == "trader_dan"
        assert user_data["role"] == "EDITOR"
        assert "hashed_password" not in user_data  # Credential redaction

        # Cookie Verification
        assert "tradepro_session" in client.cookies
        assert "tradepro_csrf" in client.cookies
        sess_csrf = client.cookies.get("tradepro_csrf")

        # 8. Test /me Endpoint
        me_res = client.get("/api/v1/auth/me")
        assert me_res.status_code == 200
        assert me_res.json()["email"] == "dan@example.com"

        # 9. Test Logout
        logout_res = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": sess_csrf, "Origin": "http://localhost:3000"}
        )
        assert logout_res.status_code == 200

        # 10. Post-logout /me should be 401
        me_after_logout = client.get("/api/v1/auth/me")
        assert me_after_logout.status_code == 401
    finally:
        app.dependency_overrides.clear()

def test_login_rate_limiting(session):
    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        csrf_res = client.get("/api/v1/auth/csrf-token")
        csrf_token = csrf_res.json()["csrf_token"]

        for _ in range(5):
            res = client.post(
                "/api/v1/auth/login",
                json={"username_or_email": "rate_limited_user", "password": "WrongPassword123!"},
                headers={"X-CSRF-Token": csrf_token, "Origin": "http://localhost:3000"}
            )
            assert res.status_code in (401, 429)

        # 6th request must trigger 429
        res_429 = client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "rate_limited_user", "password": "WrongPassword123!"},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://localhost:3000"}
        )
        assert res_429.status_code == 429
        assert "Retry-After" in res_429.headers
    finally:
        app.dependency_overrides.clear()
