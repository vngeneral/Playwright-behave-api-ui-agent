"""
Test Data Factory
=================
Centralised access layer for all test data files.

Usage:
    from utils.data_factory import DataFactory

    df = DataFactory()
    user = df.get_valid_user(0)              # first valid user
    form = df.get_form_submission("standard_order")
    endpoints = df.get_api_endpoints("get")  # list of GET endpoint configs

All files are read lazily and cached after first load.
"""
from __future__ import annotations

import json
import os
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from helpers.constants.framework_constants import TestData
from utils.misc import load_config


class DataFactory:
    """Load, cache, and expose structured test data."""

    def __init__(self):
        cfg = load_config()
        td_cfg = cfg.get("test_data", {})
        self._users_file = td_cfg.get("users_file", TestData.USERS_FILE)
        self._form_file = td_cfg.get("form_data_file", TestData.FORM_DATA_FILE)
        self._api_file = td_cfg.get("api_scenarios_file", TestData.API_SCENARIOS_FILE)

    # ------------------------------------------------------------------
    # Internal loader
    # ------------------------------------------------------------------

    def _load(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Test data file not found: {p.resolve()}")
        return json.loads(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def get_valid_user(self, index: int = 0) -> dict[str, Any]:
        """Return a valid user dict by index (wraps around)."""
        users = self._load(self._users_file)["valid_users"]
        return users[index % len(users)]

    def get_random_user(self) -> dict[str, Any]:
        users = self._load(self._users_file)["valid_users"]
        return random.choice(users)

    def get_edge_case_user(self, index: int = 0) -> dict[str, Any]:
        users = self._load(self._users_file)["edge_case_users"]
        return users[index % len(users)]

    def all_valid_users(self) -> list[dict[str, Any]]:
        return self._load(self._users_file)["valid_users"]

    # ------------------------------------------------------------------
    # Form data
    # ------------------------------------------------------------------

    def get_form_submission(self, scenario: str) -> dict[str, Any]:
        """
        Return a specific form submission scenario by name.
        Raises KeyError if not found.
        """
        submissions = self._load(self._form_file)["contact_form"]["valid_submissions"]
        for item in submissions:
            if item["scenario"] == scenario:
                return item
        raise KeyError(f"Form scenario '{scenario}' not found in {self._form_file}")

    def get_special_char_comment(self) -> str:
        return (
            self._load(self._form_file)["contact_form"]
            ["special_character_inputs"]["comments"]
        )

    def get_valid_pizza_sizes(self) -> list[str]:
        return self._load(self._form_file)["contact_form"]["validation"]["pizza_sizes"]

    # ------------------------------------------------------------------
    # API scenarios
    # ------------------------------------------------------------------

    def get_api_endpoints(self, category: str) -> list[dict[str, Any]]:
        """
        Args:
            category: "get_endpoints" | "post_endpoints" | "error_endpoints"
        """
        data = self._load(self._api_file)
        key = category if category.endswith("_endpoints") else f"{category}_endpoints"
        if key not in data:
            raise KeyError(f"API category '{key}' not found. Available: {list(data.keys())}")
        return data[key]

    def get_api_endpoint_by_id(self, endpoint_id: str) -> dict[str, Any]:
        data = self._load(self._api_file)
        for category in data.values():
            if isinstance(category, list):
                for ep in category:
                    if ep.get("id") == endpoint_id:
                        return ep
        raise KeyError(f"API endpoint '{endpoint_id}' not found")

    # ------------------------------------------------------------------
    # Scenario Outline helpers — return flat rows for Examples tables
    # ------------------------------------------------------------------

    def form_submissions_as_rows(self) -> list[tuple[str, str, str, str, str]]:
        """
        Returns (name, phone, email, size, comments) tuples
        for use in Scenario Outline Examples.
        """
        subs = self._load(self._form_file)["contact_form"]["valid_submissions"]
        return [
            (s["name"], s["phone"], s["email"], s["pizza_size"], s.get("comments", ""))
            for s in subs
        ]
