"""
Unit tests for agent/testrail/case_sync.py
No network calls — parsing/tag-insertion are pure functions, and
sync_feature_file/sync_feature_dir are tested with an injected mock/fake
TestRailClient.
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
    sync_feature_dir,
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


class _FakeClient:
    """
    In-memory TestRail stand-in that actually tracks created cases, so
    get_cases() reflects add_case() calls made earlier in the same test —
    unlike a MagicMock with a static return_value, this exercises the real
    duplicate-case check the way the live API would.
    """

    def __init__(self, start_id=900001):
        self._cases: list[dict] = []
        self._next_id = start_id
        self.add_case_calls: list[dict] = []

    def add_case(self, section_id, title, custom_steps=None):
        case = {"id": self._next_id, "title": title, "section_id": section_id}
        self._cases.append(case)
        self.add_case_calls.append(
            {"section_id": section_id, "title": title, "custom_steps": custom_steps}
        )
        self._next_id += 1
        return {"id": case["id"]}

    def get_cases(self, project_id, section_id=None, suite_id=None):
        return [
            c for c in self._cases
            if section_id is None or c["section_id"] == section_id
        ]

    def seed(self, case_id, title, section_id):
        self._cases.append({"id": case_id, "title": title, "section_id": section_id})


class TestSyncFeatureFile(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.feature_path = Path(self._tmp.name) / "x.feature"
        self.feature_path.write_text(_FEATURE, encoding="utf-8")

    def _make_client(self, start_id=900001):
        return _FakeClient(start_id=start_id)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            sync_feature_file("/does/not/exist.feature", section_id=1, project_id=1)

    def test_missing_project_id_raises(self):
        client = self._make_client()
        with self.assertRaises(ValueError):
            sync_feature_file(str(self.feature_path), section_id=42, client=client)

    def test_creates_cases_only_for_untagged_scenarios(self):
        client = self._make_client()
        report = sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        self.assertEqual(len(report.created), 2)
        self.assertEqual(report.skipped,
                         ["Registration with missing VIN returns 400"])
        self.assertEqual(len(client.add_case_calls), 2)

    def test_add_case_called_with_section_title_and_steps(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        first_call = client.add_case_calls[0]
        self.assertEqual(first_call["section_id"], 42)
        self.assertEqual(first_call["title"], "Successful vehicle registration")
        self.assertIn("valid VIN", first_call["custom_steps"])

    def test_tags_written_back_to_file(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        reparsed = find_scenarios(self.feature_path.read_text(encoding="utf-8"))
        self.assertEqual(reparsed[0].case_ids, ["900001"])
        self.assertEqual(reparsed[1].case_ids, ["448337"])
        self.assertEqual(reparsed[2].case_ids, ["900002"])

    def test_resync_is_idempotent(self):
        client = self._make_client()
        sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        report2 = sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        self.assertEqual(report2.created, [])
        self.assertEqual(len(report2.skipped), 3)
        self.assertEqual(len(client.add_case_calls), 2)   # no new calls on 2nd run

    def test_dry_run_makes_no_calls_and_no_edits(self):
        client = self._make_client()
        original = self.feature_path.read_text(encoding="utf-8")
        report = sync_feature_file(
            str(self.feature_path), section_id=42, dry_run=True, client=client
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(len(report.created), 2)
        self.assertEqual(client.add_case_calls, [])
        self.assertEqual(self.feature_path.read_text(encoding="utf-8"), original)

    def test_partial_failure_tags_successes_and_reports_failures(self):
        client = MagicMock()
        client.get_cases.return_value = []
        client.add_case.side_effect = [
            {"id": 900001},
            TestRailAPIError(status_code=400, context="add_case", body="bad request"),
        ]
        report = sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.failed), 1)
        self.assertFalse(report.ok)
        reparsed = find_scenarios(self.feature_path.read_text(encoding="utf-8"))
        self.assertEqual(reparsed[0].case_ids, ["900001"])   # success still tagged
        self.assertEqual(reparsed[2].case_ids, [])           # failure left untagged

    # -- Duplicate-case prevention ------------------------------------------

    def test_links_to_existing_case_instead_of_creating_duplicate(self):
        client = self._make_client()
        client.seed(case_id=555, title="Successful vehicle registration", section_id=42)
        report = sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)

        self.assertEqual(report.linked, [{"title": "Successful vehicle registration", "case_id": "555"}])
        self.assertEqual(len(report.created), 1)   # only the outline scenario was created
        # add_case was never called for the title that already existed
        titles_created = [c["title"] for c in client.add_case_calls]
        self.assertNotIn("Successful vehicle registration", titles_created)

    def test_linked_scenario_still_gets_tagged_in_file(self):
        client = self._make_client()
        client.seed(case_id=555, title="Successful vehicle registration", section_id=42)
        sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)

        reparsed = find_scenarios(self.feature_path.read_text(encoding="utf-8"))
        self.assertEqual(reparsed[0].case_ids, ["555"])

    def test_existing_case_title_match_is_case_and_whitespace_insensitive(self):
        client = self._make_client()
        client.seed(case_id=555, title="  successful VEHICLE registration  ", section_id=42)
        report = sync_feature_file(str(self.feature_path), section_id=42, project_id=7, client=client)
        self.assertEqual(len(report.linked), 1)

    def test_two_scenarios_sharing_a_title_in_one_run_link_to_the_same_new_case(self):
        """A duplicate title *within* the same sync run must not create two cases."""
        dup_feature = textwrap.dedent("""\
            Feature: Dup

              Scenario: Same title
                When a

              Scenario: Same title
                When b
            """)
        path = Path(self._tmp.name) / "dup.feature"
        path.write_text(dup_feature, encoding="utf-8")

        client = self._make_client()
        report = sync_feature_file(str(path), section_id=42, project_id=7, client=client)

        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.linked), 1)
        self.assertEqual(len(client.add_case_calls), 1)


class TestSyncFeatureDir(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_path = Path(self._tmp.name)

    def _write(self, name, text):
        (self.dir_path / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def test_missing_dir_raises(self):
        with self.assertRaises(NotADirectoryError):
            sync_feature_dir("/does/not/exist", section_id=1, project_id=1)

    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(sync_feature_dir(str(self.dir_path), section_id=1, project_id=1), [])

    def test_syncs_every_feature_file_in_filename_order(self):
        self._write("b.feature", """\
            Feature: B
              Scenario: B scenario
                When b
            """)
        self._write("a.feature", """\
            Feature: A
              Scenario: A scenario
                When a
            """)
        client = _FakeClient()
        reports = sync_feature_dir(str(self.dir_path), section_id=42, project_id=7, client=client)

        self.assertEqual(len(reports), 2)
        self.assertTrue(Path(reports[0].feature_path).name.startswith("a."))
        self.assertTrue(Path(reports[1].feature_path).name.startswith("b."))
        self.assertEqual(sum(len(r.created) for r in reports), 2)

    def test_shared_title_across_files_links_instead_of_duplicating(self):
        self._write("a.feature", """\
            Feature: A
              Scenario: Shared title
                When a
            """)
        self._write("b.feature", """\
            Feature: B
              Scenario: Shared title
                When b
            """)
        client = _FakeClient()
        reports = sync_feature_dir(str(self.dir_path), section_id=42, project_id=7, client=client)

        created = sum(len(r.created) for r in reports)
        linked = sum(len(r.linked) for r in reports)
        self.assertEqual(created, 1)
        self.assertEqual(linked, 1)
        self.assertEqual(len(client.add_case_calls), 1)

    def test_one_bad_file_does_not_abort_the_batch(self):
        self._write("good.feature", """\
            Feature: Good
              Scenario: Good scenario
                When good
            """)
        self._write("bad.feature", """\
            Feature: Bad
              Scenario: Bad scenario
                When bad
            """)

        class _FailingOnBadClient(_FakeClient):
            def add_case(self, section_id, title, custom_steps=None):
                if title == "Bad scenario":
                    raise TestRailAPIError(status_code=500, context="add_case", body="boom")
                return super().add_case(section_id, title, custom_steps)

        client = _FailingOnBadClient()
        reports = sync_feature_dir(str(self.dir_path), section_id=42, project_id=7, client=client)

        self.assertEqual(len(reports), 2)
        by_name = {Path(r.feature_path).name: r for r in reports}
        self.assertTrue(by_name["good.feature"].ok)
        self.assertFalse(by_name["bad.feature"].ok)

    def test_dry_run_makes_no_calls_and_no_edits(self):
        self._write("a.feature", """\
            Feature: A
              Scenario: A scenario
                When a
            """)
        original = (self.dir_path / "a.feature").read_text(encoding="utf-8")
        client = _FakeClient()
        reports = sync_feature_dir(str(self.dir_path), section_id=42, dry_run=True, client=client)

        self.assertEqual(len(reports), 1)
        self.assertEqual(client.add_case_calls, [])
        self.assertEqual((self.dir_path / "a.feature").read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
