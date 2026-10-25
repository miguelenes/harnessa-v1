from pathlib import Path

import yaml


def _load_litellm_config():
    config_path = Path(__file__).parents[1] / "infra" / "litellm" / "config.yaml"
    return yaml.safe_load(config_path.read_text())


def test_litellm_routes_pin_providers_and_disable_proxy_fallbacks():
    config = _load_litellm_config()

    deployments = {deployment["model_name"]: deployment["litellm_params"] for deployment in config["model_list"]}

    assert deployments["harnessa-deepseek-flash"] == {
        "model": "deepseek/deepseek-flash",
        "api_base": "https://api.deepseek.com",
        "api_key": "os.environ/DEEPSEEK_API_KEY",
        "timeout": 60,
    }
    assert deployments["harnessa-openrouter-deepseek-flash"] == {
        "model": "openrouter/deepseek/deepseek-v4.1-flash-1017",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "timeout": 60,
        "provider": {"order": ["deepseek"], "allow_fallbacks": False},
    }
    assert config["router_settings"]["num_retries"] == 0
    assert config["router_settings"]["max_fallbacks"] == 0


def test_litellm_does_not_log_messages():
    config = _load_litellm_config()

    assert config["litellm_settings"]["set_verbose"] is False
    assert config["litellm_settings"]["turn_off_message_logging"] is True


def test_compose_passes_provider_keys_database_url_and_master_key():
    compose_path = Path(__file__).parents[1] / "infra" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text())
    environment = compose["services"]["litellm"]["environment"]

    assert set(environment) == {
        "DEEPSEEK_API_KEY",
        "LITELLM_DATABASE_URL",
        "LITELLM_MASTER_KEY",
        "OPENROUTER_API_KEY",
    }
    assert "${LITELLM_POSTGRES_PASSWORD:" in environment["LITELLM_DATABASE_URL"]
    assert "${LITELLM_MASTER_KEY:" in environment["LITELLM_MASTER_KEY"]


def test_compose_binds_operator_ports_to_loopback():
    compose_path = Path(__file__).parents[1] / "infra" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text())

    for service in ("db", "redis", "api", "web"):
        assert all(port.startswith("127.0.0.1:") for port in compose["services"][service]["ports"])

    assert set(compose["services"]["api"]["networks"]) == {"app_internal", "github_egress"}
    assert set(compose["services"]["worker"]["networks"]) == {"app_internal", "github_egress"}
    assert set(compose["services"]["litellm"]["networks"]) == {"app_internal", "provider_egress"}
