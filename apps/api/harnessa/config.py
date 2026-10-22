from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://harnessa:harnessa@localhost:5432/harnessa"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = ""
    bootstrap_token: str = ""
    allowed_origins: str = "http://localhost:3000"
    cookie_secure: bool = False
    repository_targets: str = ""


settings = Settings()
