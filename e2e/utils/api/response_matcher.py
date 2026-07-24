"""
Response Matcher — expected-vs-actual JSON assertion
====================================================
Deep comparison of an expected JSON document against an actual API response,
built for real-world APIs where parts of the response are dynamic
(server-generated IDs, timestamps, trace headers).

Three complementary tools:

1. ``assert_json_matches(expected, actual, ...)`` — deep diff that collects
   **every** mismatch (not fail-fast) and raises one readable AssertionError.
   Dynamic fields are handled two ways:

   • Matcher tokens in the *expected* document validate shape instead of value::

        {
          "transactionId": "<<uuid>>",
          "createdAt":     "<<iso8601>>",
          "status":        "<<any_of:REGISTERED|PENDING>>",
          "count":         "<<int>>",
          "message":       "<<regex:^Vehicle .+ registered$>>"
        }

   • ``ignore_paths`` skips fields entirely, with wildcards::

        ignore_paths=["headers.*", "*.traceId", "vinList[*].registeredAt"]

2. ``get_json_path(data, "a.b[0].c")`` — dotted-path extraction for
   field-level assertions and response chaining.

3. ``validate_schema(data, "name")`` — JSON Schema validation
   (test_data/schemas/<name>.schema.json) via the existing jsonschema dep.

Matcher tokens (usable anywhere a value appears in the expected document):

    <<any>>              key must exist; any value (including null)
    <<ignore>>           always passes, even if the key is absent
    <<absent>>           key must NOT exist in the actual document
    <<non_null>>         present and not null
    <<non_empty>>        present, not null, and len() > 0 (str/list/dict)
    <<string>> <<int>> <<number>> <<bool>> <<list>> <<object>> <<null>>
                         type checks (<<number>> = int or float)
    <<uuid>>             lowercase-insensitive UUID
    <<iso8601>>          ISO-8601 date-time (offset or 'Z' or none)
    <<date>>             YYYY-MM-DD
    <<regex:PATTERN>>    full-match regex on str(value)
    <<contains:X>>       substring (str) or membership (list)
    <<starts_with:X>> <<ends_with:X>>
    <<any_of:a|b|c>>     value equals one of the alternatives (string compare)
    <<len:N>>            len(value) == N
    <<gt:N>> <<gte:N>> <<lt:N>> <<lte:N>>   numeric comparisons
    <<between:A:B>>      A <= value <= B

Comparison modes:
    subset (default)  — extra keys in the actual response are allowed; APIs
                        may add fields without breaking tests.
    strict_keys=True  — unexpected keys in actual objects are mismatches too.
"""
from __future__ import annotations

import json
import re
import uuid as _uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logger import log_failure, log_info_emoji

try:
    import allure
    _ALLURE_AVAILABLE = True
except ImportError:
    _ALLURE_AVAILABLE = False

__all__ = [
    "Mismatch",
    "match_json",
    "assert_json_matches",
    "get_json_path",
    "validate_schema",
    "JsonPathError",
]

_SENTINEL = object()


# ---------------------------------------------------------------------------
# Mismatch record
# ---------------------------------------------------------------------------

@dataclass
class Mismatch:
    path: str
    expected: Any
    actual: Any
    reason: str

    def __str__(self) -> str:
        exp = "(absent)" if self.expected is _SENTINEL else json.dumps(self.expected, default=str)
        act = "(absent)" if self.actual is _SENTINEL else json.dumps(self.actual, default=str)
        return f"  {self.path or '$'}: {self.reason}\n    expected: {exp}\n    actual:   {act}"


# ---------------------------------------------------------------------------
# ignore_paths — wildcard path patterns
# ---------------------------------------------------------------------------
# Actual paths look like:  json.vinList[0].vin
# Patterns support:
#   *      any single segment name            headers.*
#   [*]    any list index                     vinList[*].createdAt
#   **     any run of segments (incl. none)   **.transactionId

_SEG_RE = re.compile(r"^([^\[\]]*)((?:\[(?:\d+|\*)\])*)$")


def _split_path(path: str) -> list[str]:
    """'a.b[0].c' → ['a', 'b', '[0]', 'c'] — indices become their own segments."""
    segments: list[str] = []
    for part in path.split("."):
        if not part:
            continue
        m = _SEG_RE.match(part)
        if not m:
            segments.append(part)
            continue
        name, indices = m.groups()
        if name:
            segments.append(name)
        for idx in re.findall(r"\[(?:\d+|\*)\]", indices):
            segments.append(idx)
    return segments


def _segments_match(pattern: Sequence[str], path: Sequence[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_segments_match(rest, path[i:]) for i in range(len(path) + 1))
    if not path:
        return False
    seg = path[0]
    if head == "*" and not seg.startswith("["):
        return _segments_match(rest, path[1:])
    if head == "[*]" and seg.startswith("["):
        return _segments_match(rest, path[1:])
    if head == seg:
        return _segments_match(rest, path[1:])
    return False


def _is_ignored(path: str, ignore_patterns: Iterable[Sequence[str]]) -> bool:
    segs = _split_path(path)
    return any(_segments_match(p, segs) for p in ignore_patterns)


# ---------------------------------------------------------------------------
# Matcher tokens
# ---------------------------------------------------------------------------

_MATCHER_RE = re.compile(r"^<<\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?::(.*?))?\s*>>$", re.DOTALL)

_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "int":    lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool":   lambda v: isinstance(v, bool),
    "list":   lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null":   lambda v: v is None,
}


def _is_valid_uuid(value: Any) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return None


def _check_matcher(name: str, arg: str | None, actual: Any, path: str) -> Mismatch | None:
    """Return None when the matcher passes, a Mismatch when it fails."""
    token = f"<<{name}{':' + arg if arg is not None else ''}>>"

    def fail(reason: str) -> Mismatch:
        return Mismatch(path, token, actual, reason)

    if name == "ignore":
        return None
    if name == "absent":
        return fail("expected key to be absent") if actual is not _SENTINEL else None

    if actual is _SENTINEL:
        return fail("key missing from actual response")

    if name == "any":
        return None

    if name == "non_null":
        return fail("expected non-null value") if actual is None else None
    if name == "non_empty":
        if actual is None or not hasattr(actual, "__len__") or len(actual) == 0:
            return fail("expected a non-empty value")
        return None
    if name in _TYPE_CHECKS:
        return None if _TYPE_CHECKS[name](actual) else fail(f"expected type {name}")
    if name == "uuid":
        return None if _is_valid_uuid(actual) else fail("not a valid UUID")
    if name == "iso8601":
        if isinstance(actual, str) and _ISO8601_RE.match(actual):
            return None
        return fail("not an ISO-8601 date-time")
    if name == "date":
        if isinstance(actual, str) and _DATE_RE.match(actual):
            try:
                datetime.strptime(actual, "%Y-%m-%d")
                return None
            except ValueError:
                pass
        return fail("not a YYYY-MM-DD date")
    if name == "regex":
        if arg is None:
            return fail("regex matcher needs a pattern")
        return None if re.fullmatch(arg, str(actual)) else fail(f"does not match /{arg}/")
    if name == "contains":
        if isinstance(actual, str):
            return None if (arg or "") in actual else fail(f"does not contain '{arg}'")
        if isinstance(actual, (list, tuple)):
            if arg in actual or any(str(item) == arg for item in actual):
                return None
            return fail(f"list does not contain '{arg}'")
        return fail("contains only works on strings and lists")
    if name == "starts_with":
        return None if str(actual).startswith(arg or "") else fail(f"does not start with '{arg}'")
    if name == "ends_with":
        return None if str(actual).endswith(arg or "") else fail(f"does not end with '{arg}'")
    if name == "any_of":
        options = (arg or "").split("|")
        if str(actual) in options or actual in options:
            return None
        return fail(f"not one of {options}")
    if name == "len":
        if not hasattr(actual, "__len__"):
            return fail("value has no length")
        try:
            expected_len = int(arg or "")
        except ValueError:
            return fail(f"len matcher needs an integer, got '{arg}'")
        return None if len(actual) == expected_len else fail(f"length is {len(actual)}, expected {expected_len}")
    if name in ("gt", "gte", "lt", "lte"):
        actual_num, bound = _as_number(actual), _as_number(arg)
        if actual_num is None or bound is None:
            return fail(f"{name} needs numeric values")
        passed = {
            "gt": actual_num > bound, "gte": actual_num >= bound,
            "lt": actual_num < bound, "lte": actual_num <= bound,
        }[name]
        return None if passed else fail(f"expected value {name} {arg}")
    if name == "between":
        parts = (arg or "").split(":")
        low = _as_number(parts[0]) if len(parts) == 2 else None
        high = _as_number(parts[1]) if len(parts) == 2 else None
        actual_num = _as_number(actual)
        if low is None or high is None or actual_num is None:
            return fail("between needs <<between:LOW:HIGH>> with numeric values")
        return None if low <= actual_num <= high else fail(f"not between {low:g} and {high:g}")

    return fail(f"unknown matcher token <<{name}>>")


# ---------------------------------------------------------------------------
# Deep comparison
# ---------------------------------------------------------------------------

def match_json(
    expected: Any,
    actual: Any,
    *,
    ignore_paths: Iterable[str] = (),
    strict_keys: bool = False,
) -> list[Mismatch]:
    """
    Deep-compare ``expected`` against ``actual`` and return ALL mismatches.

    Args:
        expected:     document that may contain ``<<matcher>>`` tokens.
        actual:       parsed API response body.
        ignore_paths: path patterns to skip (see module docstring).
        strict_keys:  when True, keys present in actual but not in expected
                      are reported as mismatches.
    """
    patterns = [_split_path(p) for p in ignore_paths]
    mismatches: list[Mismatch] = []
    _compare(expected, actual, "", patterns, strict_keys, mismatches)
    return mismatches


def _compare(
    expected: Any,
    actual: Any,
    path: str,
    ignore_patterns: list[list[str]],
    strict_keys: bool,
    out: list[Mismatch],
) -> None:
    if path and _is_ignored(path, ignore_patterns):
        return

    # Matcher token?
    if isinstance(expected, str):
        m = _MATCHER_RE.match(expected)
        if m:
            mismatch = _check_matcher(m.group(1), m.group(2), actual, path)
            if mismatch:
                out.append(mismatch)
            return

    if actual is _SENTINEL:
        out.append(Mismatch(path, expected, _SENTINEL, "key missing from actual response"))
        return

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            out.append(Mismatch(path, expected, actual,
                                f"expected object, got {type(actual).__name__}"))
            return
        for key, exp_value in expected.items():
            child = f"{path}.{key}" if path else key
            _compare(exp_value, actual.get(key, _SENTINEL), child,
                     ignore_patterns, strict_keys, out)
        if strict_keys:
            for key in actual:
                if key not in expected:
                    child = f"{path}.{key}" if path else key
                    if not _is_ignored(child, ignore_patterns):
                        out.append(Mismatch(child, _SENTINEL, actual[key],
                                            "unexpected key in actual response"))
        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            out.append(Mismatch(path, expected, actual,
                                f"expected list, got {type(actual).__name__}"))
            return
        if len(expected) != len(actual):
            out.append(Mismatch(path, expected, actual,
                                f"list length {len(actual)}, expected {len(expected)}"))
        for i, exp_item in enumerate(expected):
            if i >= len(actual):
                break
            _compare(exp_item, actual[i], f"{path}[{i}]",
                     ignore_patterns, strict_keys, out)
        return

    # Scalars — bool is not int for test purposes
    if isinstance(expected, bool) != isinstance(actual, bool) or expected != actual:
        out.append(Mismatch(path, expected, actual, "values differ"))


def assert_json_matches(
    expected: Any,
    actual: Any,
    *,
    ignore_paths: Iterable[str] = (),
    strict_keys: bool = False,
    label: str = "response body",
) -> None:
    """
    Assert that ``actual`` matches ``expected``; raise one AssertionError
    listing every mismatch. Attaches the full diff to Allure on failure.

    ``expected`` may be a JSON string (e.g. a Gherkin doc-string) or a
    parsed structure.
    """
    if isinstance(expected, str):
        expected = json.loads(expected)

    mismatches = match_json(
        expected, actual, ignore_paths=ignore_paths, strict_keys=strict_keys
    )
    if not mismatches:
        log_info_emoji("✅", f"{label} matches expected ({_count_leaves(expected)} checks)")
        return

    report = (
        f"{label} does not match expected — {len(mismatches)} mismatch(es):\n"
        + "\n".join(str(m) for m in mismatches)
    )
    log_failure(report)
    _attach_diff(expected, actual, report)
    raise AssertionError(report)


def _count_leaves(doc: Any) -> int:
    if isinstance(doc, dict):
        return sum(_count_leaves(v) for v in doc.values()) or 1
    if isinstance(doc, list):
        return sum(_count_leaves(v) for v in doc) or 1
    return 1


def _attach_diff(expected: Any, actual: Any, report: str) -> None:
    if not _ALLURE_AVAILABLE:
        return
    try:
        allure.attach(
            f"{report}\n\n--- expected ---\n{json.dumps(expected, indent=2, default=str)}"
            f"\n\n--- actual ---\n{json.dumps(actual, indent=2, default=str)[:8000]}",
            name="Response mismatch diff",
            attachment_type=allure.attachment_type.TEXT,
        )
    except Exception:
        pass  # Allure attach must never crash a test


# ---------------------------------------------------------------------------
# JSON path extraction — for field assertions and response chaining
# ---------------------------------------------------------------------------

class JsonPathError(AssertionError):
    """Raised when a dotted path cannot be resolved in a document."""


def get_json_path(data: Any, path: str) -> Any:
    """
    Extract a value via dotted path: ``"a.b[0].c"`` (a leading ``$.`` is allowed).

    Raises JsonPathError with the failing segment when the path does not resolve.
    """
    clean = path[2:] if path.startswith("$.") else path.lstrip("$")
    current = data
    walked = "$"
    for seg in _split_path(clean):
        if seg.startswith("["):
            index = int(seg[1:-1])
            if not isinstance(current, list):
                raise JsonPathError(
                    f"Path '{path}': {walked} is {type(current).__name__}, not a list"
                )
            if index >= len(current):
                raise JsonPathError(
                    f"Path '{path}': index {index} out of range at {walked} (len {len(current)})"
                )
            current = current[index]
            walked += seg
        else:
            if not isinstance(current, dict):
                raise JsonPathError(
                    f"Path '{path}': {walked} is {type(current).__name__}, not an object"
                )
            if seg not in current:
                raise JsonPathError(
                    f"Path '{path}': key '{seg}' not found at {walked}. "
                    f"Available keys: {sorted(current.keys())}"
                )
            current = current[seg]
            walked += f".{seg}"
    return current


# ---------------------------------------------------------------------------
# JSON Schema validation
# ---------------------------------------------------------------------------

def validate_schema(data: Any, schema_name: str, schemas_dir: str | Path | None = None) -> None:
    """
    Validate ``data`` against ``test_data/schemas/<schema_name>.schema.json``.

    Raises AssertionError listing every schema violation (not just the first).
    """
    import jsonschema

    from helpers.constants.framework_constants import TestData

    directory = Path(schemas_dir) if schemas_dir else Path(TestData.SCHEMAS_DIR)
    schema_path = directory / f"{schema_name}.schema.json"
    if not schema_path.exists():
        raise AssertionError(
            f"Schema '{schema_name}' not found at {schema_path.resolve()}"
        )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [
            f"  {'.'.join(str(p) for p in err.absolute_path) or '$'}: {err.message}"
            for err in errors
        ]
        report = (
            f"Response violates schema '{schema_name}' — {len(errors)} error(s):\n"
            + "\n".join(lines)
        )
        log_failure(report)
        _attach_diff(schema, data, report)
        raise AssertionError(report)
    log_info_emoji("✅", f"Response conforms to schema '{schema_name}'")
