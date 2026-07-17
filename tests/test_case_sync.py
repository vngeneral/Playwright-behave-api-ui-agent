"""
Unit tests for agent/testrail/case_sync.py
No network calls — parsing/tag-insertion are pure functions, and
sync_feature_file is tested with an injected mock TestRailClient.
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.testrail.case_sync import (
    find_scenarios,
    insert_case_tags,
    sync_feature_file,
)
from agent.testrail.client import TestRailAPIError

_FEATURE = textwrap.dedent("""\
    Feature: Vehicle registration

      Background:
        Given the vehicle API client is initialised

      @api @ai_generated
      Scenario: Successful vehicle registration
        When the user registers a vehicle with a valid VIN
        Then the response status code should be 200
        And the response should contain a transaction id

      @api @testrail_C448337
      Scenario: Registration with missing VIN returns 400
        When the user registers a vehicle without a VIN
        Then the response status code should be 400

      @api
      Scenario Outline: Registration with invalid partner code <code>
        When the user registers a vehicle with partner code "<code>"
        Then the response status code should be 422

        Examples:
          | code    |
          | BAD-001 |
""")


class TestFindScenarios(unittest.TestCase):
    def setUp(self):
        self.scenarios = find_scenarios(_FEATURE)

    def test_finds_all_scenarios_including_outline(self):
        self.assertEqual(len(self.scenarios), 3)
        self.assertEqual(self.scenarios[0].title, "Successful vehicle registration")
        self.assertEqual(self.scenarios[2].title,
                         "Registration with invalid partner code <code>")

    def test_background_is_not_a_scenario(self):
        titles = [s.title for s in self.scenarios]
        self.assertNotIn("the vehicle API client is initialised", titles)

    def test_tags_collected(self):
        self.assertIn("api", self.scenarios[0].tags)
        self.assertIn("ai_generated", self.scenarios[0].tags)

    def test_existing_testrail_tag_detected(self):
        self.assertEqual(self.scenarios[0].case_ids, [])
        self.assertEqual(self.scenarios[1].case_ids, ["448337"])
        self.assertEqual(self.scenarios[2].case_ids, [])

    def test_body_contains_steps(self):
        body = self.scenarios[0].body
        self.assertIn("When the user registers a vehicle with a valid VIN", body)
        self.assertIn("Then the response status code should be 200", body)

    def test_body_stops_at_next_scenario(self):
        self.assertNotIn("without a VIN", self.scenarios[0].body)

    def test_outline_body_includes_examples(self):
        body = self.scenarios[2].body
        self.assertIn("Examples:", body)
        self.assertIn("BAD-001", body)


class TestInsertCaseTags(unittest.TestCase):
    def test_inserts_tag_above_scenario_with_matching_indent(self):
        scenarios = find_scenarios(_FEATURE)
        updated = insert_case_tags(_FEATURE, {scenarios[0].line_idx: "999001"})
        lines = updated.splitlines()
        scenario_line = lines.index("  Scenario: Successful vehicle registration")
        self.assertEqual(lines[scenario_line - 1], "  @testrail_C999001")

    def test_other_lines_preserved(self):
        scenarios = find_scenarios(_FEATURE)
        updated = insert_case_tags(_FEATURE, {scenarios[0].line_idx: "999001"})
        for line in _FEATURE.splitlines():
            self.assertIn(line, updated.splitlines())

    def test_multiple_insertions_do_not_shift_each_other(self):
        scenarios = find_scenarios(_FEATURE)
        updated = insert_case_tags(_FEATURE, {
            scenarios[0].line_idx: "111",
            scenarios[2].line_idx: "333",
        })
        reparsed = find_scenarios(updated)
        self.assertEqual(reparsed[0].case_ids, ["111"])
        self.assertEqual(reparsed[1].case_ids, ["448337"])
        self.assertEqual(reparsed[2].case_ids, ["333"])


class TestSyncFeatureFile(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.feature_path = Path(self._tmp.name) / "x.feature"
        self.feature_path.write_text(_FEATURE, encoding="utf-8")

    def _make_client(self, start_id=900001):
        client = MagicMock()
        counter = iter(range(start_id, start_id + 100))
        client.add_case.side_effect = lambda **_: {"id": next(counter)}
        return client

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            sync_feature_file("/does/not/exist.feature", section_id=1)

    def test_creates_cases_only_for_untagged_scenarios(self):
        client = self._make_client()
        report = sync_feature_file(str(self.feature_path), section_id=42, client=client)
        self.assertEqual(len(report.created), 2)
        self.assertEqual(report.skipped,
                         ["Registration with missing VIN returns 400"])
        self.assertEqual(client.add_case.call_count, 2)

    def test_add_case_called_with_section_title_and_steps(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, client=client)
        first_call = client.add_case.call_args_list[0].kwargs
        self.assertEqual(first_call["section_id"], 42)
        self.assertEqual(first_call["title"], "Successful vehicle registration")
        self.assertIn("valid VIN", first_call["custom_steps"])

    def test_tags_written_back_to_file(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, client=client)
        reparsed = find_scenarios(self.feature_path.read_text(encoding="utf-8"))
        self.assertEqual(reparsed[0].case_ids, ["900001"])
        self.assertEqual(reparsed[1].case_ids, ["448337"])
        self.assertEqual(reparsed[2].case_ids, ["900002"])

    def test_resync_is_idempotent(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, client=client)
        report2 = sync_feature_file(str(self.feature_path), section_id=42, client=client)
        self.assertEqual(report2.created, [])
        self.assertEqual(len(report2.skipped), 3)
        self.assertEqual(client.add_case.call_count, 2)   # no new calls on 2nd run

    def test_dry_run_makes_no_calls_and_no_edits(self):
        client = self._make_client()
        original = self.feature_path.read_text(encoding="utf-8")
        report = sync_feature_file(
            str(self.feature_path), section_id=42, dry_run=True, client=client
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(len(report.created), 2)
        client.add_case.assert_not_called()
        self.assertEqual(self.feature_path.read_text(encoding="utf-8"), original)

    def test_partial_failure_tags_successes_and_reports_failures(self):
        client = MagicMock()
        client.add_case.side_effect = [
            {"id": 900001},
            TestRailAPIError(status_code=400, context="add_case", body="bad request"),
        ]
        report = sync_feature_file(str(self.feature_path), section_id=42, client=client)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.failed), 1)
        self.assertFalse(report.ok)
        reparsed = find_scenarios(self.feature_path.read_text(encoding="utf-8"))
        self.assertEqual(reparsed[0].case_ids, ["900001"])   # success still tagged
        self.assertEqual(reparsed[2].case_ids, [])           # failure left untagged


if __name__ == "__main__":
    unittest.main()
