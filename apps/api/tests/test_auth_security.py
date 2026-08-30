import pytest
from fastapi import HTTPException
from src.auth.security import hash_password, verify_password

def test_hash_password_valid():
    pwd = "ValidSecurePassword123!"
    h = hash_password(pwd)
    assert h.startswith("$argon2id$")

    is_valid, needs_rehash = verify_password(pwd, h)
    assert is_valid is True
    assert needs_rehash is False

def test_verify_password_incorrect():
    pwd = "ValidSecurePassword123!"
    h = hash_password(pwd)

    is_valid, needs_rehash = verify_password("WrongPassword123!", h)
    assert is_valid is False
    assert needs_rehash is False

def test_verify_password_malformed_hash_no_500():
    # Malformed hashes should return (False, False) gracefully without crashing
    assert verify_password("Password123", "not_a_hash") == (False, False)
    assert verify_password("Password123", "!DISABLED_PRINCIPAL") == (False, False)
    assert verify_password("Password123", "$argon2id$invalid_format") == (False, False)

def test_password_length_bounds():
    # Under minimum (< 8)
    with pytest.raises(HTTPException) as exc_info:
        hash_password("short")
    assert exc_info.value.status_code == 400

    # Over maximum (> 128)
    with pytest.raises(HTTPException) as exc_info:
        hash_password("A" * 129)
    assert exc_info.value.status_code == 400

def test_password_rehash_detection():
    # Hash with low time_cost
    from argon2 import PasswordHasher
    old_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    old_hash = old_hasher.hash("RehashTestPassword123")

    is_valid, needs_rehash = verify_password("RehashTestPassword123", old_hash)
    assert is_valid is True
    assert needs_rehash is True
