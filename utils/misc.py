import os
import yaml
from helpers.constants.framework_constants import CONFIG_YAML


def load_config() -> dict:
    """Load config.yaml. CONFIG_YAML is an absolute path built from CWD."""
    if os.path.exists(CONFIG_YAML):
        with open(CONFIG_YAML, 'r') as f:
            return yaml.safe_load(f) or {}
    return {}


def get_active_env(config: dict) -> str:
    """Return the active environment name: ENV envvar > config key > 'dev'."""
    return os.getenv("ENV", config.get("environment", "dev"))


def get_env_config(config: dict) -> dict:
    """Return the environment-specific sub-config block."""
    env = get_active_env(config)
    envs = config.get("environments", {})
    if env not in envs:
        raise ValueError(
            f"Environment '{env}' not found in config.yaml. "
            f"Available: {list(envs.keys())}"
        )
    return envs[env]
