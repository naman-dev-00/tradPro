import re
from typing import Tuple
from email_validator import validate_email as _validate_email, EmailNotValidError
from fastapi import HTTPException, status

USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")

def normalize_username(username: str) -> Tuple[str, str]:
    """
    Validates and normalizes username.
    Returns: (trimmed_display_username, normalized_lookup_username)
    """
    if not isinstance(username, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be a string."
        )

    trimmed = username.strip()
    if not USERNAME_REGEX.match(trimmed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-50 ASCII characters matching '^[A-Za-z0-9_.-]+$'."
        )

    return trimmed, trimmed.casefold()

def normalize_email(email: str, test_environment: bool = False) -> Tuple[str, str]:
    """
    Validates and normalizes email address.
    Returns: (normalized_display_email, normalized_lookup_email)
    """
    if not isinstance(email, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email must be a string."
        )

    trimmed = email.strip()
    try:
        validated = _validate_email(trimmed, check_deliverability=False, test_environment=test_environment)
        display_email = validated.normalized
        lookup_email = display_email.casefold()
        return display_email, lookup_email
    except EmailNotValidError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid email format: {str(e)}"
        )
