import os
from typing import Optional

class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "local").lower()
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")
    PORT: int = int(os.getenv("PORT", 8000))
    FALLBACK_DB_URL: str = "sqlite:///./tradepro.db"

settings = Settings()
