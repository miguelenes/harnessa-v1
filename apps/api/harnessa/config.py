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
    max_concurrent_runs: int = 4
    run_lease_seconds: int = 30
    run_heartbeat_seconds: int = 10
    run_max_attempts: int = 3
    run_max_wall_seconds: int = 1800
    event_retention_days: int = 30
    event_replay_limit: int = 500
    redis_stream_maxlen: int = 10000
    model_gateway_url: str = ""
    model_max_attempts: int = 3
    model_http_timeout_seconds: float = 60.0
    model_cooldown_seconds: int = 30
    model_circuit_breaker_failures: int = 2
    model_max_retry_after_seconds: int = 15
    model_reservation_lease_seconds: int = 180
    model_interactive_concurrency: int = 4
    model_interactive_reserve: int = 2
    model_probe_overflow_limit: int = 0


settings = Settings()
