import secrets
import hashlib
from typing import Optional
from urllib.parse import urlparse
from src.config import settings

def generate_preauth_csrf_token() -> str:
    """Generates a cryptographically secure random token for login CSRF protection."""
    return secrets.token_urlsafe(32)

def verify_csrf_hash(submitted_token: str, expected_hash: str) -> bool:
    """
    Computes SHA-256 of submitted raw token and compares with stored hash using constant-time comparison.
    """
    if not submitted_token or not expected_hash:
        return False
    submitted_hash = hashlib.sha256(submitted_token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(submitted_hash, expected_hash)

def validate_origin(origin_or_referer: Optional[str]) -> bool:
    """
    Validates request Origin or Referer header against allowed origins whitelist.
    """
    if not origin_or_referer:
        # In strictly browser-enforced mutations, missing origin/referer is rejected
        return False

    parsed = urlparse(origin_or_referer)
    # Reconstruct scheme + host + port
    netloc = parsed.netloc
    scheme = parsed.scheme
    if not scheme or not netloc:
        return False

    origin_base = f"{scheme}://{netloc}"
    return origin_base in settings.ALLOWED_ORIGINS
