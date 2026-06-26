from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://meduser:medpass@localhost:5432/medarchive"
    redis_url: str = "redis://localhost:6379/0"
    upload_dir: str = "./uploads"

    # Normalization thresholds
    auto_match_threshold: float = 85.0   # rapidfuzz score 0-100
    review_threshold: float = 60.0       # below this → unmatched

    # Price anomaly check: flag if new price differs > X% from previous
    price_anomaly_pct: float = 50.0

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
