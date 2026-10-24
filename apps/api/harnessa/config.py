from decimal import Decimal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://harnessa:harnessa@localhost:5432/harnessa"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = ""
    bootstrap_token: str = ""
    metrics_token: SecretStr | None = None
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
    mcp_servers_json: str = "{}"
    mcp_read_only_tools_json: str = "{}"
    mcp_secret_references_json: str = "{}"
    github_secret_references_json: str = "{}"
    github_api_url: str = "https://api.github.com"
    github_http_timeout_seconds: float = 10.0
    github_webhook_max_bytes: int = 262144
    github_webhook_max_attempts: int = 12
    retrieval_approved_skill_paths_json: str = "{}"
    model_max_attempts: int = 3
    model_http_timeout_seconds: float = 60.0
    model_cooldown_seconds: int = 30
    model_circuit_breaker_failures: int = 2
    model_max_retry_after_seconds: int = 15
    model_reservation_lease_seconds: int = 180
    model_interactive_concurrency: int = 4
    model_interactive_reserve: int = 2
    model_probe_overflow_limit: int = 0
    model_global_budget_usd: Decimal = Field(default=Decimal("20.00"), gt=0)
    artifact_root: str = "/var/lib/harnessa/artifacts"
    acp_execution_enabled: bool = False
    acp_repository_sources_json: str = "{}"
    acp_workspace_root: str = "/var/lib/harnessa/workspaces"
    acp_runner_state_root: str = "/var/lib/harnessa/runner"
    acp_agent_image: str = (
        "ghcr.io/anomalyco/opencode:2.0.32@sha256:"
        "85986f556e985e377bd24cd68a5ccfbc647bb49e591194edda8a0e4542303319"
    )
    acp_harness_revision: str = "repository-default-v1"
    acp_cpu_limit: float = 2.0
    acp_memory_limit_bytes: int = 2147483648
    acp_pids_limit: int = 256
    acp_tmpfs_limit_bytes: int = 536870912
    acp_workspace_limit_bytes: int = 4294967296
    verification_workspace_limit_bytes: int = Field(default=1073741824, ge=16777216, le=4294967296)
    acp_frame_limit_bytes: int = 1048576
    acp_update_limit: int = 10000
    acp_artifact_limit_bytes: int = 10485760
    acp_model_broker_enabled: bool = False
    acp_model_max_request_bytes: int = Field(default=524288, ge=1024, le=1048576)
    acp_model_alias: str | None = None
    acp_model_max_output_tokens: int = 8192
    openspec_repository_sources_json: str = "{}"
    openspec_workspace_root: str = "/var/lib/harnessa/specifications"
    openspec_node_path: str = ""
    openspec_cli_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    openspec_model_alias: str | None = None
    openspec_model_max_output_tokens: int = Field(default=4096, ge=256, le=16_384)
    openspec_require_signoff: bool = False
    harness2_max_output_tokens: int = Field(default=4096, ge=256, le=16_384)
    harness2_max_probe_concurrency: int = Field(default=3, ge=1, le=3)
    harness2_holdout_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    harness_holdout_evaluator_url: str = ""
    harness_holdout_evaluator_token: SecretStr | None = None


settings = Settings()
