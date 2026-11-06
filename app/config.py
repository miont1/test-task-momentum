from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/helpdesk"
    redis_url: str = "redis://redis:6379/0"
    openai_api_key: str = "mock"
    llm_api_key: str = "mock"
    openai_model: str = "gpt-4o-mini"
    llm_rate_limit_per_minute: int = 60
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def effective_api_key(self) -> str:
        for key in (self.openai_api_key, self.llm_api_key):
            cleaned = (key or "").strip()
            if cleaned and cleaned.lower() != "mock":
                return cleaned
        return "mock"


settings = Settings()
