import os
from typing import Optional, List

class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "local").lower()
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")
    PORT: int = int(os.getenv("PORT", 8000))
    FALLBACK_DB_URL: str = "sqlite:///./tradepro.db"

    # Security & Cookie Settings
    COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "false" if APP_ENV in ("local", "test") else "true").lower() in ("true", "1", "yes")
    COOKIE_SAMESITE: str = os.getenv("COOKIE_SAMESITE", "lax").lower()
    COOKIE_DOMAIN: Optional[str] = os.getenv("COOKIE_DOMAIN")

    # Session Lifetimes
    SESSION_IDLE_TIMEOUT_MINUTES: int = int(os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", "30"))
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = int(os.getenv("SESSION_ABSOLUTE_TIMEOUT_HOURS", "12"))
    # CORS & Origin Validation Whitelist
    ALLOWED_ORIGINS: List[str] = [
        origin.strip() for origin in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",") if origin.strip()
    ]

    def verify_security_settings(self) -> None:
        """Enforces security constraints on application startup."""
        if self.APP_ENV in ("staging", "production"):
            if not self.COOKIE_SECURE:
                raise RuntimeError(
                    f"Production/staging environment '{self.APP_ENV}' requires secure cookies (COOKIE_SECURE=True)."
                )

settings = Settings()
