"""Secure server-side credential encryption and token masking abstraction.

Keeps broker tokens encrypted at rest with the encryption key maintained
strictly outside the database in environment configuration. Never exposes
plaintext secrets or tokens in logs, responses, exception chains, or storage.
"""
import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("tradepro.broker.secret_store")


class BrokerKeyNotConfiguredError(RuntimeError):
    """Raised when encryption is requested but BROKER_SECRET_KEY is not configured."""
    pass


class BrokerKeyInvalidError(ValueError):
    """Raised when configured BROKER_SECRET_KEY is not a valid 32-byte Fernet key."""
    pass


def validate_fernet_key(key_input: str) -> bytes:
    """Validates that key_input is a valid 32-byte urlsafe-base64 encoded Fernet key."""
    if not key_input or not isinstance(key_input, str):
        raise BrokerKeyInvalidError("BROKER_SECRET_KEY must be a non-empty string.")
    try:
        decoded = base64.urlsafe_b64decode(key_input.encode("ascii"))
        if len(decoded) != 32:
            raise BrokerKeyInvalidError(
                f"BROKER_SECRET_KEY must decode to exactly 32 bytes (got {len(decoded)} bytes)."
            )
        return key_input.encode("ascii")
    except Exception as e:
        if isinstance(e, BrokerKeyInvalidError):
            raise
        raise BrokerKeyInvalidError("BROKER_SECRET_KEY is not valid urlsafe-base64.") from None


class BrokerSecretStore:
    """Provides authenticated symmetric encryption for broker credentials with lazy key initialization."""

    def __init__(self, key: Optional[str] = None):
        self._explicit_key = key
        self._cached_fernet: Optional[Fernet] = None

    def _get_fernet(self) -> Fernet:
        """Lazily initializes and returns the Fernet cipher instance."""
        if self._cached_fernet is not None:
            return self._cached_fernet

        raw_key = self._explicit_key or os.environ.get("BROKER_SECRET_KEY")
        if not raw_key or not raw_key.strip():
            raise BrokerKeyNotConfiguredError(
                "BROKER_SECRET_KEY is not configured in environment. "
                "A properly generated 32-byte Fernet key is required before storing broker credentials."
            )

        valid_key_bytes = validate_fernet_key(raw_key.strip())
        self._cached_fernet = Fernet(valid_key_bytes)
        return self._cached_fernet

    def encrypt(self, plaintext: str) -> str:
        """Encrypts a plaintext token string and returns an ASCII ciphertext."""
        if not plaintext:
            raise ValueError("Cannot encrypt empty token")
        fernet = self._get_fernet()
        ciphertext_bytes = fernet.encrypt(plaintext.encode("utf-8"))
        return ciphertext_bytes.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypts a ciphertext token and returns the plaintext string."""
        if not ciphertext:
            raise ValueError("Cannot decrypt empty ciphertext")
        fernet = self._get_fernet()
        try:
            decrypted_bytes = fernet.decrypt(ciphertext.encode("ascii"))
            return decrypted_bytes.decode("utf-8")
        except InvalidToken:
            logger.error("Failed to decrypt broker credential: invalid token or wrong key")
            raise ValueError("Decryption failed: corrupted credential or wrong key") from None

    @staticmethod
    def mask_account_id(account_id: Optional[str]) -> str:
        """Masks broker client ID / UCC for secure display (e.g. UCC***345)."""
        if not account_id:
            return "NOT_SET"
        stripped = str(account_id).strip()
        if len(stripped) <= 3:
            return "***"
        return f"***{stripped[-3:]}"

    @staticmethod
    def mask_token(token: Optional[str]) -> str:
        """Returns safe redacted token representation."""
        if not token:
            return "NONE"
        return "[REDACTED_ACCESS_TOKEN]"


# Module-level default instance (lazy initialization ensures no crash on unconfigured import)
secret_store = BrokerSecretStore()
