"""
Unit tests — utils/api/response_matcher.py (e2e layer)
======================================================
    python -m pytest tests/test_response_matcher.py -v
"""
from __future__ import annotations

import json

import pytest

from utils.api.response_matcher import (
    JsonPathError,
    assert_json_matches,
    get_json_path,
    match_json,
    validate_schema,
)


def paths(mismatches):
    return [m.path for m in mismatches]


# ---------------------------------------------------------------------------
# Exact matching + mismatch collection
# ---------------------------------------------------------------------------

class TestExactMatching:
    def test_identical_documents_match(self):
        doc = {"a": 1, "b": {"c": [1, 2]}, "d": None}
        assert match_json(doc, doc) == []

    def test_value_difference_reported_with_path(self):
        result = match_json({"a": {"b": 1}}, {"a": {"b": 2}})
        assert paths(result) == ["a.b"]
        assert result[0].reason == "values differ"

    def test_all_mismatches_collected_not_fail_fast(self):
        result = match_json({"a": 1, "b": 2, "c": 3}, {"a": 9, "b": 9, "c": 9})
        assert sorted(paths(result)) == ["a", "b", "c"]

    def test_missing_key_reported(self):
        result = match_json({"a": 1, "b": 2}, {"a": 1})
        assert paths(result) == ["b"]
        assert "missing" in result[0].reason

    def test_subset_mode_allows_extra_actual_keys(self):
        assert match_json({"a": 1}, {"a": 1, "extra": "ok"}) == []

    def test_strict_keys_flags_extra_actual_keys(self):
        result = match_json({"a": 1}, {"a": 1, "extra": "no"}, strict_keys=True)
        assert paths(result) == ["extra"]
        assert "unexpected key" in result[0].reason

    def test_type_mismatch_object_vs_scalar(self):
        result = match_json({"a": {"b": 1}}, {"a": 5})
        assert "expected object" in result[0].reason

    def test_bool_is_not_int(self):
        assert match_json({"a": True}, {"a": 1}) != []
        assert match_json({"a": 1}, {"a": True}) != []

    def test_null_matches_null_only(self):
        assert match_json({"a": None}, {"a": None}) == []
        assert match_json({"a": None}, {"a": 0}) != []


class TestListMatching:
    def test_lists_compared_by_index(self):
        result = match_json({"v": [1, 2, 3]}, {"v": [1, 9, 3]})
        assert paths(result) == ["v[1]"]

    def test_list_length_mismatch_reported(self):
        result = match_json({"v": [1, 2]}, {"v": [1]})
        assert any("list length" in m.reason for m in result)

    def test_nested_objects_in_lists(self):
        expected = {"vinList": [{"vin": "ABC"}, {"vin": "DEF"}]}
        actual = {"vinList": [{"vin": "ABC"}, {"vin": "XXX"}]}
        assert paths(match_json(expected, actual)) == ["vinList[1].vin"]

    def test_expected_list_vs_actual_scalar(self):
        result = match_json({"v": [1]}, {"v": "nope"})
        assert "expected list" in result[0].reason


# ---------------------------------------------------------------------------
# Matcher tokens — dynamic field validation
# ---------------------------------------------------------------------------

class TestMatcherTokens:
    def test_any_passes_for_any_value_including_null(self):
        assert match_json({"a": "<<any>>"}, {"a": None}) == []
        assert match_json({"a": "<<any>>"}, {"a": 123}) == []

    def test_any_fails_when_key_absent(self):
        assert match_json({"a": "<<any>>"}, {}) != []

    def test_ignore_passes_even_when_absent(self):
        assert match_json({"a": "<<ignore>>"}, {}) == []

    def test_absent_requires_key_missing(self):
        assert match_json({"a": "<<absent>>"}, {}) == []
        assert match_json({"a": "<<absent>>"}, {"a": 1}) != []

    def test_non_null(self):
        assert match_json({"a": "<<non_null>>"}, {"a": 0}) == []
        assert match_json({"a": "<<non_null>>"}, {"a": None}) != []

    def test_non_empty(self):
        assert match_json({"a": "<<non_empty>>"}, {"a": [1]}) == []
        assert match_json({"a": "<<non_empty>>"}, {"a": ""}) != []
        assert match_json({"a": "<<non_empty>>"}, {"a": {}}) != []

    def test_type_matchers(self):
        actual = {"s": "x", "i": 3, "f": 1.5, "b": True, "l": [], "o": {}, "n": None}
        expected = {
            "s": "<<string>>", "i": "<<int>>", "f": "<<number>>",
            "b": "<<bool>>", "l": "<<list>>", "o": "<<object>>", "n": "<<null>>",
        }
        assert match_json(expected, actual) == []

    def test_int_matcher_rejects_bool(self):
        assert match_json({"a": "<<int>>"}, {"a": True}) != []

    def test_uuid_matcher(self):
        assert match_json({"a": "<<uuid>>"}, {"a": "64a1a656-130c-4aec-8a04-b73b3d2515e0"}) == []
        assert match_json({"a": "<<uuid>>"}, {"a": "not-a-uuid"}) != []

    def test_iso8601_matcher(self):
        good = ["2026-07-24T09:15:02Z", "2026-07-24T09:15:02.123+02:00", "2026-07-24 09:15:02"]
        for value in good:
            assert match_json({"t": "<<iso8601>>"}, {"t": value}) == [], value
        assert match_json({"t": "<<iso8601>>"}, {"t": "24/07/2026"}) != []

    def test_date_matcher(self):
        assert match_json({"d": "<<date>>"}, {"d": "2026-07-24"}) == []
        assert match_json({"d": "<<date>>"}, {"d": "2026-13-45"}) != []

    def test_regex_matcher_full_match(self):
        assert match_json({"m": "<<regex:^V-\\d+$>>"}, {"m": "V-42"}) == []
        assert match_json({"m": "<<regex:^V-\\d+$>>"}, {"m": "xV-42"}) != []

    def test_contains_on_string_and_list(self):
        assert match_json({"u": "<<contains:/post>>"}, {"u": "https://x/post"}) == []
        assert match_json({"l": "<<contains:b>>"}, {"l": ["a", "b"]}) == []
        assert match_json({"l": "<<contains:z>>"}, {"l": ["a", "b"]}) != []

    def test_starts_and_ends_with(self):
        assert match_json({"a": "<<starts_with:veh>>"}, {"a": "vehicle"}) == []
        assert match_json({"a": "<<ends_with:cle>>"}, {"a": "vehicle"}) == []
        assert match_json({"a": "<<starts_with:x>>"}, {"a": "vehicle"}) != []

    def test_any_of(self):
        assert match_json({"s": "<<any_of:REGISTERED|PENDING>>"}, {"s": "PENDING"}) == []
        assert match_json({"s": "<<any_of:REGISTERED|PENDING>>"}, {"s": "FAILED"}) != []
        assert match_json({"code": "<<any_of:200|201>>"}, {"code": 201}) == []

    def test_len_matcher(self):
        assert match_json({"v": "<<len:2>>"}, {"v": [1, 2]}) == []
        assert match_json({"v": "<<len:2>>"}, {"v": [1]}) != []

    def test_numeric_comparisons(self):
        assert match_json({"n": "<<gte:5>>"}, {"n": 5}) == []
        assert match_json({"n": "<<gt:5>>"}, {"n": 5}) != []
        assert match_json({"n": "<<lt:10>>"}, {"n": 9.5}) == []
        assert match_json({"n": "<<between:1:5>>"}, {"n": 3}) == []
        assert match_json({"n": "<<between:1:5>>"}, {"n": 6}) != []

    def test_unknown_matcher_reports_mismatch(self):
        result = match_json({"a": "<<bogus>>"}, {"a": 1})
        assert "unknown matcher" in result[0].reason

    def test_literal_string_that_is_not_a_token(self):
        assert match_json({"a": "<not a token>"}, {"a": "<not a token>"}) == []


# ---------------------------------------------------------------------------
# ignore_paths — wildcard skipping of dynamic fields
# ---------------------------------------------------------------------------

class TestIgnorePaths:
    def test_exact_path_ignored(self):
        expected = {"a": 1, "createdAt": "stale"}
        actual = {"a": 1, "createdAt": "fresh"}
        assert match_json(expected, actual, ignore_paths=["createdAt"]) == []

    def test_nested_path_ignored(self):
        expected = {"data": {"ts": "old", "id": 1}}
        actual = {"data": {"ts": "new", "id": 1}}
        assert match_json(expected, actual, ignore_paths=["data.ts"]) == []

    def test_single_star_matches_one_segment(self):
        expected = {"headers": {"Host": "a", "Trace": "x"}}
        actual = {"headers": {"Host": "b", "Trace": "y"}}
        assert match_json(expected, actual, ignore_paths=["headers.*"]) == []

    def test_list_index_wildcard(self):
        expected = {"vinList": [{"vin": "A", "at": "t1"}, {"vin": "B", "at": "t2"}]}
        actual = {"vinList": [{"vin": "A", "at": "x"}, {"vin": "B", "at": "y"}]}
        assert match_json(expected, actual, ignore_paths=["vinList[*].at"]) == []

    def test_double_star_matches_any_depth(self):
        expected = {"a": {"b": {"traceId": "1"}}, "traceId": "2"}
        actual = {"a": {"b": {"traceId": "9"}}, "traceId": "8"}
        assert match_json(expected, actual, ignore_paths=["**.traceId"]) == []

    def test_ignored_missing_key_is_not_a_mismatch(self):
        assert match_json({"a": 1, "gone": 2}, {"a": 1}, ignore_paths=["gone"]) == []

    def test_non_ignored_mismatches_still_reported(self):
        expected = {"a": 1, "ts": "old"}
        actual = {"a": 2, "ts": "new"}
        result = match_json(expected, actual, ignore_paths=["ts"])
        assert paths(result) == ["a"]

    def test_strict_keys_respects_ignore(self):
        result = match_json({"a": 1}, {"a": 1, "origin": "1.2.3.4"},
                            ignore_paths=["origin"], strict_keys=True)
        assert result == []


# ---------------------------------------------------------------------------
# assert_json_matches — error reporting
# ---------------------------------------------------------------------------

class TestAssertJsonMatches:
    def test_passes_silently_on_match(self):
        assert_json_matches({"a": 1}, {"a": 1})

    def test_accepts_json_string_expected(self):
        assert_json_matches('{"a": "<<int>>"}', {"a": 5})

    def test_raises_with_all_mismatches_listed(self):
        with pytest.raises(AssertionError) as err:
            assert_json_matches({"a": 1, "b": "<<uuid>>"}, {"a": 2, "b": "nope"})
        message = str(err.value)
        assert "2 mismatch(es)" in message
        assert "a:" in message and "b:" in message

    def test_error_shows_expected_and_actual(self):
        with pytest.raises(AssertionError) as err:
            assert_json_matches({"status": "OK"}, {"status": "FAILED"})
        assert "OK" in str(err.value) and "FAILED" in str(err.value)


# ---------------------------------------------------------------------------
# get_json_path
# ---------------------------------------------------------------------------

class TestGetJsonPath:
    DOC = {"json": {"vinList": [{"vin": "ABC"}, {"vin": "DEF"}], "n": 0}}

    def test_simple_key(self):
        assert get_json_path({"a": 1}, "a") == 1

    def test_nested_with_index(self):
        assert get_json_path(self.DOC, "json.vinList[1].vin") == "DEF"

    def test_leading_dollar_prefix_allowed(self):
        assert get_json_path(self.DOC, "$.json.n") == 0

    def test_missing_key_lists_available(self):
        with pytest.raises(JsonPathError, match="Available keys"):
            get_json_path(self.DOC, "json.missing")

    def test_index_out_of_range(self):
        with pytest.raises(JsonPathError, match="out of range"):
            get_json_path(self.DOC, "json.vinList[9].vin")

    def test_indexing_a_non_list(self):
        with pytest.raises(JsonPathError, match="not a list"):
            get_json_path(self.DOC, "json.n[0]")


# ---------------------------------------------------------------------------
# validate_schema
# ---------------------------------------------------------------------------

class TestValidateSchema:
    @pytest.fixture()
    def schemas_dir(self, tmp_path):
        schema = {
            "type": "object",
            "required": ["id", "status"],
            "properties": {
                "id": {"type": "string"},
                "status": {"enum": ["OK", "FAILED"]},
            },
        }
        (tmp_path / "thing.schema.json").write_text(json.dumps(schema))
        return tmp_path

    def test_valid_document_passes(self, schemas_dir):
        validate_schema({"id": "x", "status": "OK"}, "thing", schemas_dir=schemas_dir)

    def test_all_violations_reported(self, schemas_dir):
        with pytest.raises(AssertionError) as err:
            validate_schema({"id": 5}, "thing", schemas_dir=schemas_dir)
        message = str(err.value)
        assert "2 error(s)" in message
        assert "status" in message and "id" in message

    def test_missing_schema_file_raises(self, schemas_dir):
        with pytest.raises(AssertionError, match="not found"):
            validate_schema({}, "nope", schemas_dir=schemas_dir)
