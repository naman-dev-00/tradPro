import pytest
from fastapi import HTTPException
from src.auth.normalization import normalize_username, normalize_email

def test_normalize_username_valid():
    disp, norm = normalize_username("  Alice_Trader.99  ")
    assert disp == "Alice_Trader.99"
    assert norm == "alice_trader.99"

def test_normalize_username_invalid_chars():
    with pytest.raises(HTTPException) as exc_info:
        normalize_username("user@name")
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        normalize_username("ab")  # Too short (< 3)
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        normalize_username("a" * 51)  # Too long (> 50)
    assert exc_info.value.status_code == 400

def test_normalize_email_valid():
    disp, norm = normalize_email("  User.Name+Test@Example.COM  ")
    assert disp == "User.Name+Test@example.com"
    assert norm == "user.name+test@example.com"

def test_normalize_email_invalid():
    with pytest.raises(HTTPException) as exc_info:
        normalize_email("not-an-email")
    assert exc_info.value.status_code == 400

def test_normalize_email_test_environment_flag():
    # By default test_environment=False
    disp, norm = normalize_email("normal.user@example.org")
    assert norm == "normal.user@example.org"

    # Explicit test_environment=True can be passed if needed in test suites
    disp_test, norm_test = normalize_email("test.user@custom.test", test_environment=True)
    assert norm_test == "test.user@custom.test"
