"""
Payload Builder — smart dynamic test-data generation
====================================================
Turns a JSON template (dict / list / JSON string) into a concrete request
payload by resolving ``{{token}}`` placeholders at build time.

Why templates instead of hardcoded payloads:
  • Every run gets fresh unique data (no duplicate-key collisions on re-runs)
  • Feature files stay readable — the *intent* ("a fresh uuid") is visible
  • Values captured from earlier responses can be injected (request chaining)

Usage::

    from utils.api.payload_builder import build_payload

    payload = build_payload({
        "transactionId": "{{uuid}}",
        "createdAt":     "{{now}}",
        "vinList":       [{"vin": "{{random_vin}}"}],
        "quantity":      "{{random_int:1:5}}",
        "ref":           "{{saved:order_id}}",
    }, store=context.saved_values)

Supported tokens (whole-value tokens keep their native type — e.g.
``"{{random_int:1:5}}"`` becomes an ``int``; tokens embedded inside a longer
string are interpolated as text):

    {{uuid}}                  lowercase UUID4
    {{transaction_id}}        alias of {{uuid}} — semantic marker for txn IDs
    {{timestamp}}             epoch seconds (int)
    {{timestamp_ms}}          epoch milliseconds (int)
    {{now}}                   ISO-8601 UTC, e.g. 2026-07-24T09:15:02Z
    {{now:+2d}} {{now:-30m}}  ISO-8601 UTC with offset (s/m/h/d)
    {{today}}                 YYYY-MM-DD
    {{today:+1d}}             date with day offset
    {{random_int:MIN:MAX}}    random integer in [MIN, MAX] (int)
    {{random_string:N}}       N random letters+digits
    {{random_digits:N}}       N random digits (string, may lead with 0)
    {{random_choice:a|b|c}}   one of the given alternatives
    {{random_email}}          unique test email
    {{random_vin}}            valid ISO-3779 VIN (17 chars, check digit correct)
    {{env:VAR}}               environment variable (empty string if unset)
    {{env:VAR:default}}       environment variable with fallback
    {{saved:key}}             value from the provided store (request chaining)

Transaction-ID note: ``{{transaction_id}}`` resolves at *build* time — before
the request fires — which honours the project rule that IDs are generated
explicitly before body construction. For VehicleAPIClient keep using
``generate_transaction_id()``; this token is for generic payload templates.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import string
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["build_payload", "resolve_tokens", "deep_merge", "generate_vin", "PayloadTokenError"]


class PayloadTokenError(ValueError):
    """Raised when a template contains an unknown or malformed token."""


# ---------------------------------------------------------------------------
# VIN generation — ISO 3779 with a correct position-9 check digit
# ---------------------------------------------------------------------------

# I, O, Q are illegal in VINs
_VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
_VIN_TRANSLITERATION = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    **{str(d): d for d in range(10)},
}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def _vin_check_digit(vin17: str) -> str:
    total = sum(
        _VIN_TRANSLITERATION[ch] * weight
        for ch, weight in zip(vin17, _VIN_WEIGHTS, strict=True)
    )
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def generate_vin() -> str:
    """Generate a random, structurally valid ISO-3779 VIN (check digit correct)."""
    chars = [random.choice(_VIN_CHARS) for _ in range(17)]
    chars[8] = "0"  # placeholder while computing the check digit
    chars[8] = _vin_check_digit("".join(chars))
    return "".join(chars)


# ---------------------------------------------------------------------------
# Token resolvers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?::((?:[^{}])*?))?\s*\}\}")

_OFFSET_RE = re.compile(r"^([+-])(\d+)([smhd])$")
_OFFSET_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _parse_offset(arg: str | None) -> timedelta:
    if not arg:
        return timedelta()
    m = _OFFSET_RE.match(arg.strip())
    if not m:
        raise PayloadTokenError(
            f"Invalid time offset '{arg}' — expected e.g. '+2d', '-30m', '+90s'"
        )
    sign, amount, unit = m.groups()
    delta = timedelta(**{_OFFSET_UNITS[unit]: int(amount)})
    return delta if sign == "+" else -delta


def _resolve_one(name: str, arg: str | None, store: Mapping[str, Any]) -> Any:
    """Resolve a single token to its (possibly non-string) value."""
    if name in ("uuid", "transaction_id"):
        return str(uuid.uuid4())
    if name == "timestamp":
        return int(datetime.now(UTC).timestamp())
    if name == "timestamp_ms":
        return int(datetime.now(UTC).timestamp() * 1000)
    if name == "now":
        moment = datetime.now(UTC) + _parse_offset(arg)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    if name == "today":
        moment = datetime.now(UTC) + _parse_offset(arg)
        return moment.strftime("%Y-%m-%d")
    if name == "random_int":
        try:
            low, high = (int(p) for p in (arg or "").split(":", 1))
        except ValueError:
            raise PayloadTokenError(
                f"random_int needs MIN:MAX, got '{arg}' — e.g. {{{{random_int:1:100}}}}"
            ) from None
        return random.randint(low, high)
    if name == "random_string":
        length = int(arg) if arg and arg.isdigit() else 8
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))
    if name == "random_digits":
        length = int(arg) if arg and arg.isdigit() else 6
        return "".join(random.choices(string.digits, k=length))
    if name == "random_choice":
        options = [o for o in (arg or "").split("|") if o != ""]
        if not options:
            raise PayloadTokenError("random_choice needs options — e.g. {{random_choice:a|b}}")
        return random.choice(options)
    if name == "random_email":
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"qa+{local}@example.com"
    if name == "random_vin":
        return generate_vin()
    if name == "env":
        if not arg:
            raise PayloadTokenError("env token needs a variable name — e.g. {{env:MY_VAR}}")
        var, _, default = arg.partition(":")
        return os.getenv(var, default)
    if name == "saved":
        if not arg:
            raise PayloadTokenError("saved token needs a key — e.g. {{saved:order_id}}")
        if arg not in store:
            raise PayloadTokenError(
                f"No saved value named '{arg}'. Available: {sorted(store.keys()) or '(none)'}"
            )
        return store[arg]
    raise PayloadTokenError(f"Unknown payload token '{{{{{name}}}}}'")


def _resolve_string(value: str, store: Mapping[str, Any]) -> Any:
    """Resolve tokens in one string. A whole-string token keeps its native type."""
    whole = _TOKEN_RE.fullmatch(value.strip())
    if whole:
        return _resolve_one(whole.group(1), whole.group(2), store)
    return _TOKEN_RE.sub(
        lambda m: str(_resolve_one(m.group(1), m.group(2), store)), value
    )


def resolve_tokens(value: Any, store: Mapping[str, Any] | None = None) -> Any:
    """Recursively resolve ``{{token}}`` placeholders in any JSON-like structure."""
    store = store or {}
    if isinstance(value, str):
        return _resolve_string(value, store)
    if isinstance(value, dict):
        return {k: resolve_tokens(v, store) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tokens(item, store) for item in value]
    return value


# ---------------------------------------------------------------------------
# Deep merge + public entry point
# ---------------------------------------------------------------------------

def deep_merge(base: dict, overrides: Mapping[str, Any]) -> dict:
    """Return a new dict — ``overrides`` merged into ``base`` recursively.

    Dicts merge key-by-key; every other type (including lists) is replaced.
    """
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(merged.get(key), dict) and isinstance(value, Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_payload(
    template: dict | list | str,
    overrides: Mapping[str, Any] | None = None,
    store: Mapping[str, Any] | None = None,
) -> Any:
    """
    Build a concrete payload from a template.

    Args:
        template:  dict / list, or a JSON string (e.g. a Gherkin doc-string).
        overrides: values deep-merged on top of the template *before* token
                   resolution — override values may themselves contain tokens.
        store:     saved values for ``{{saved:key}}`` (e.g. context.saved_values).

    Returns:
        The payload with every token resolved. Input is never mutated.
    """
    if isinstance(template, str):
        try:
            template = json.loads(template)
        except json.JSONDecodeError as exc:
            raise PayloadTokenError(f"Payload template is not valid JSON: {exc}") from exc
    else:
        template = copy.deepcopy(template)

    if overrides:
        if not isinstance(template, dict):
            raise PayloadTokenError("overrides are only supported for dict templates")
        template = deep_merge(template, overrides)

    return resolve_tokens(template, store)
