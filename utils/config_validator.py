"""
Config Schema Validator
=======================
Validates resources/config.yaml against a JSON Schema at startup so
misconfigured environments fail fast with a clear error, not a
cryptic KeyError mid-test.

Usage:
    from utils.config_validator import validate_config
    validate_config(config_dict)   # raises ConfigValidationError on failure
"""
from __future__ import annotations

import jsonschema
from jsonschema import Draft7Validator


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["environment", "environments"],
    "additionalProperties": True,
    "properties": {
        "environment": {
            "type": "string",
            "enum": ["dev", "staging", "prod"],
            "description": "Active environment — overridden by ENV env-var",
        },
        "ai_model": {"type": "string"},
        "browser": {
            "type": "object",
            "properties": {
                "default": {"type": "string", "enum": ["chromium", "firefox", "webkit"]},
                "headless": {"type": "boolean"},
                "timeout_ms": {"type": "integer", "minimum": 1000},
                "navigation_timeout_ms": {"type": "integer", "minimum": 1000},
            },
            "additionalProperties": True,
        },
        "environments": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "required": ["base_url"],
                "properties": {
                    "base_url": {
                        "type": "string",
                        "pattern": r"^https?://",
                        "description": "Must start with http:// or https://",
                    }
                },
                "additionalProperties": True,
            },
        },
        "timeouts": {
            "type": "object",
            "properties": {
                "default": {"type": "integer", "minimum": 0},
                "navigation": {"type": "integer", "minimum": 0},
                "network_idle": {"type": "integer", "minimum": 0},
                "page_load_threshold_s": {"type": "number", "minimum": 0},
            },
            "additionalProperties": True,
        },
        "retry": {
            "type": "object",
            "properties": {
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                "delay_seconds": {"type": "number", "minimum": 0},
                "backoff_multiplier": {"type": "number", "minimum": 1.0},
            },
            "additionalProperties": True,
        },
        "parallel": {
            "type": "object",
            "properties": {
                "default_workers": {"type": "integer", "minimum": 1, "maximum": 32},
                "priority_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "additionalProperties": True,
        },
        "reporting": {
            "type": "object",
            "properties": {
                "screenshots_on_failure": {"type": "boolean"},
                "attach_page_source": {"type": "boolean"},
                "metrics_enabled": {"type": "boolean"},
                "categories_file": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "notifications": {
            "type": "object",
            "properties": {
                "slack_enabled": {"type": "boolean"},
                "slack_webhook_env_var": {"type": "string"},
                "notify_on_failure_only": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "test_data": {
            "type": "object",
            "properties": {
                "users_file": {"type": "string"},
                "form_data_file": {"type": "string"},
                "api_scenarios_file": {"type": "string"},
                "cleanup_after_run": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ConfigValidationError(ValueError):
    """Raised when config.yaml fails schema validation."""


def validate_config(config: dict) -> None:
    """
    Validate *config* against the framework schema.

    Raises:
        ConfigValidationError: with a human-readable message listing all
                               validation failures.
    """
    validator = Draft7Validator(_CONFIG_SCHEMA)
    errors = sorted(validator.iter_errors(config), key=lambda e: list(e.path))

    if errors:
        messages = [f"  • {'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
        raise ConfigValidationError(
            "config.yaml failed validation:\n" + "\n".join(messages)
        )
