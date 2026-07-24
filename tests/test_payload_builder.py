"""
Unit tests — utils/api/payload_builder.py (e2e layer)
=====================================================
    python -m pytest tests/test_payload_builder.py -v
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from utils.api.payload_builder import (
    PayloadTokenError,
    _vin_check_digit,  # type: ignore
    build_payload,
    deep_merge,
    generate_vin,
    resolve_tokens,
)

# ---------------------------------------------------------------------------
# Token resolution — identity / uniqueness
# ---------------------------------------------------------------------------

class TestUuidTokens:
    def test_uuid_is_valid_uuid4(self):
        value = resolve_tokens("{{uuid}}")
        assert uuid.UUID(value).version == 4

    def test_transaction_id_alias(self):
        value = resolve_tokens("{{transaction_id}}")
        assert uuid.UUID(value)

    def test_two_uuids_differ(self):
        assert resolve_tokens("{{uuid}}") != resolve_tokens("{{uuid}}")

    def test_embedded_uuid_interpolates_into_string(self):
        value = resolve_tokens("order-{{uuid}}-end")
        assert value.startswith("order-") and value.endswith("-end")
        assert uuid.UUID(value[len("order-"):-len("-end")])


class TestTimeTokens:
    def test_timestamp_is_int_near_now(self):
        value = resolve_tokens("{{timestamp}}")
        assert isinstance(value, int)
        assert abs(value - datetime.now(UTC).timestamp()) < 5

    def test_timestamp_ms_is_int(self):
        value = resolve_tokens("{{timestamp_ms}}")
        assert isinstance(value, int)
        assert value > 10**12

    def test_now_is_iso8601_utc(self):
        value = resolve_tokens("{{now}}")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)

    def test_now_with_positive_day_offset(self):
        value = resolve_tokens("{{now:+2d}}")
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        delta = parsed - datetime.now(UTC)
        assert timedelta(days=1, hours=23) < delta < timedelta(days=2, minutes=1)

    def test_now_with_negative_minute_offset(self):
        value = resolve_tokens("{{now:-30m}}")
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        delta = datetime.now(UTC) - parsed
        assert timedelta(minutes=29) < delta < timedelta(minutes=31)

    def test_today_format(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve_tokens("{{today}}"))

    def test_bad_offset_raises(self):
        with pytest.raises(PayloadTokenError, match="Invalid time offset"):
            resolve_tokens("{{now:2 days}}")


class TestRandomTokens:
    def test_random_int_returns_int_in_range(self):
        for _ in range(30):
            value = resolve_tokens("{{random_int:1:5}}")
            assert isinstance(value, int)
            assert 1 <= value <= 5

    def test_random_int_without_range_raises(self):
        with pytest.raises(PayloadTokenError, match="random_int needs MIN:MAX"):
            resolve_tokens("{{random_int}}")

    def test_random_string_length(self):
        value = resolve_tokens("{{random_string:12}}")
        assert len(value) == 12 and value.isalnum()

    def test_random_digits(self):
        value = resolve_tokens("{{random_digits:6}}")
        assert len(value) == 6 and value.isdigit()

    def test_random_choice(self):
        assert resolve_tokens("{{random_choice:a|b|c}}") in ("a", "b", "c")

    def test_random_choice_no_options_raises(self):
        with pytest.raises(PayloadTokenError):
            resolve_tokens("{{random_choice}}")

    def test_random_email_shape(self):
        assert re.fullmatch(r"qa\+[a-z0-9]{10}@example\.com", resolve_tokens("{{random_email}}"))


class TestEnvAndSavedTokens:
    def test_env_reads_variable(self, monkeypatch):
        monkeypatch.setenv("PB_TEST_VAR", "hello")
        assert resolve_tokens("{{env:PB_TEST_VAR}}") == "hello"

    def test_env_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("PB_MISSING_VAR", raising=False)
        assert resolve_tokens("{{env:PB_MISSING_VAR:fallback}}") == "fallback"

    def test_env_unset_no_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("PB_MISSING_VAR", raising=False)
        assert resolve_tokens("{{env:PB_MISSING_VAR}}") == ""

    def test_saved_reads_store(self):
        assert resolve_tokens("{{saved:order_id}}", {"order_id": "abc-1"}) == "abc-1"

    def test_saved_preserves_native_type(self):
        assert resolve_tokens("{{saved:count}}", {"count": 42}) == 42

    def test_saved_missing_key_raises_with_available_keys(self):
        with pytest.raises(PayloadTokenError, match="order_id"):
            resolve_tokens("{{saved:missing}}", {"order_id": "x"})

    def test_unknown_token_raises(self):
        with pytest.raises(PayloadTokenError, match="Unknown payload token"):
            resolve_tokens("{{bogus_token}}")


# ---------------------------------------------------------------------------
# VIN generation
# ---------------------------------------------------------------------------

class TestVinGeneration:
    def test_vin_is_17_chars(self):
        assert len(generate_vin()) == 17

    def test_vin_has_no_illegal_chars(self):
        for _ in range(20):
            vin = generate_vin()
            assert not set(vin) & {"I", "O", "Q"}, vin
            assert vin.isalnum() and vin.upper() == vin

    def test_vin_check_digit_is_correct(self):
        for _ in range(20):
            vin = generate_vin()
            assert vin[8] == _vin_check_digit(vin), vin

    def test_known_vin_check_digit(self):
        # The all-ones VIN is the canonical ISO-3779 example: weights sum to
        # 89 → 89 % 11 = 1, so '11111111111111111' is self-consistent.
        assert _vin_check_digit("11111111111111111") == "1"

    def test_random_vin_token(self):
        vin = resolve_tokens("{{random_vin}}")
        assert len(vin) == 17 and vin[8] == _vin_check_digit(vin)


# ---------------------------------------------------------------------------
# build_payload — structures, JSON strings, overrides
# ---------------------------------------------------------------------------

class TestBuildPayload:
    def test_resolves_nested_dicts_and_lists(self):
        payload = build_payload({
            "id": "{{uuid}}",
            "items": [{"vin": "{{random_vin}}"}, {"vin": "{{random_vin}}"}],
            "meta": {"count": "{{random_int:2:2}}"},
        })
        assert uuid.UUID(payload["id"])
        assert len(payload["items"][0]["vin"]) == 17
        assert payload["items"][0]["vin"] != payload["items"][1]["vin"]
        assert payload["meta"]["count"] == 2

    def test_accepts_json_string_template(self):
        payload = build_payload('{"orderId": "{{uuid}}", "qty": "{{random_int:3:3}}"}')
        assert uuid.UUID(payload["orderId"])
        assert payload["qty"] == 3

    def test_invalid_json_string_raises(self):
        with pytest.raises(PayloadTokenError, match="not valid JSON"):
            build_payload("{not json}")

    def test_non_token_values_pass_through(self):
        payload = build_payload({"a": 1, "b": None, "c": True, "d": "plain"})
        assert payload == {"a": 1, "b": None, "c": True, "d": "plain"}

    def test_template_is_not_mutated(self):
        template = {"id": "{{uuid}}", "nested": {"x": "{{now}}"}}
        build_payload(template)
        assert template["id"] == "{{uuid}}"
        assert template["nested"]["x"] == "{{now}}"

    def test_overrides_deep_merge(self):
        payload = build_payload(
            {"partner": "P-1", "meta": {"env": "dev", "keep": True}},
            overrides={"meta": {"env": "staging"}},
        )
        assert payload == {"partner": "P-1", "meta": {"env": "staging", "keep": True}}

    def test_overrides_may_contain_tokens(self):
        payload = build_payload({"id": "static"}, overrides={"id": "{{uuid}}"})
        assert uuid.UUID(payload["id"])

    def test_overrides_replace_lists_wholesale(self):
        payload = build_payload({"vins": ["a", "b"]}, overrides={"vins": ["c"]})
        assert payload["vins"] == ["c"]


class TestDeepMerge:
    def test_merge_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        deep_merge(base, {"a": {"c": 2}})
        assert base == {"a": {"b": 1}}

    def test_scalar_replaces_dict(self):
        assert deep_merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}
