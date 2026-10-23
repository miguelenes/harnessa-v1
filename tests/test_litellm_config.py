from pathlib import Path

import yaml


def test_litellm_routes_pin_providers_and_disable_proxy_fallbacks():
    config_path = Path(__file__).parents[1] / "infra" / "litellm" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())

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
    config_path = Path(__file__).parents[1] / "infra" / "litellm" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())

    assert config["litellm_settings"]["set_verbose"] is False
    assert config["litellm_settings"]["turn_off_message_logging"] is True
