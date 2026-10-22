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
    artifact_root: str = "/var/lib/harnessa/artifacts"
    acp_execution_enabled: bool = False
    acp_repository_sources_json: str = "{}"
    acp_workspace_root: str = "/var/lib/harnessa/workspaces"
    acp_runner_state_root: str = "/var/lib/harnessa/runner"
    acp_agent_image: str = (
        "ghcr.io/anomalyco/opencode@sha256:"
        "85986f556e985e377bd24cd68a5ccfbc647bb49e591194edda8a0e4542303319"
    )
    acp_cpu_limit: float = 2.0
    acp_memory_limit_bytes: int = 2147483648
    acp_pids_limit: int = 256
    acp_tmpfs_limit_bytes: int = 536870912
    acp_workspace_limit_bytes: int = 4294967296
    acp_frame_limit_bytes: int = 1048576
    acp_update_limit: int = 10000
    acp_artifact_limit_bytes: int = 10485760
    acp_model_broker_enabled: bool = False
    acp_model_alias: str | None = None
    acp_model_max_output_tokens: int = 8192


settings = Settings()
