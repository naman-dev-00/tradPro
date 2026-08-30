import logging
from typing import Tuple
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError, HashingError
from fastapi import HTTPException, status

logger = logging.getLogger("tradepro.auth.security")

# RFC 9106 recommended Argon2id parameters
hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16
)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

def validate_password_length(password: str) -> None:
    if not isinstance(password, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be a string."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password exceeds maximum length of {MAX_PASSWORD_LENGTH} characters."
        )

def hash_password(password: str) -> str:
    """Hashes a password using Argon2id with bounded length enforcement."""
    validate_password_length(password)
    return hasher.hash(password)

def verify_password(password: str, hashed_password: str) -> Tuple[bool, bool]:
    """
    Verifies a password against an Argon2id hash.
    Returns: (is_valid, needs_rehash)
    Never raises cryptographic exceptions or 500 on malformed hashes.
    """
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False, False
    if not isinstance(hashed_password, str) or not hashed_password.startswith("$argon2"):
        return False, False

    try:
        is_valid = hasher.verify(hashed_password, password)
        needs_rehash = hasher.check_needs_rehash(hashed_password)
        return is_valid, needs_rehash
    except (VerificationError, InvalidHashError, HashingError):
        return False, False
    except Exception as e:
        logger.warning(f"Unexpected password verification exception: {type(e).__name__}")
        return False, False
