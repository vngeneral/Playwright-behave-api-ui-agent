"""
TestRail Integration — Unit Tests
===================================
Coverage areas:
  1. Result mapper  — Behave scenario → status id + failure-cause comment
  2. PendingStore   — lifecycle: add → get_pending → mark_pushed → clear
  3. TestRailClient — HTTP request shape and error handling (no live call)
  4. Command parser — !testrail commands recognised and routed correctly
  5. Command handler— testrail_command.py output strings for each sub-command

Run:
    python -m pytest tests/test_testrail.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Make sure project root is on the path when running from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.integrations.command_parser import parse_command
from agent.integrations.testrail_command import (
    _parse_push_args,
    handle_testrail_command,
)
from agent.testrail.client import (
    TestRailAPIError,
    TestRailClient,
    TestRailConfigError,
)
from agent.testrail.pending_store import (
    STATUS_PENDING,
    STATUS_PUSHED,
    PendingStore,
)
from agent.testrail.result_mapper import (
    TESTRAIL_FAILED,
    TESTRAIL_PASSED,
    TestRailResult,
    extract_case_ids,
    from_behave_scenario,
)

# ===========================================================================
# 1 — Result mapper: Behave scenario → TestRail result
# ===========================================================================


class TestFromBehaveScenario(unittest.TestCase):
    """from_behave_scenario() — maps a Behave-style scenario to TestRailResult."""

    def _make_scenario(self, status="passed", steps=None, name="My Scenario",
                       tags=None, error_message=None):
        scenario = SimpleNamespace(
            name=name,
            status=status,
            tags=tags or [],
            duration=3.5,
            steps=[],
            error_message=error_message,
        )
        if steps:
            for step_status, step_name, err in steps:
                scenario.steps.append(SimpleNamespace(
                    step_type="when",
                    name=step_name,
                    status=step_status,
                    error_message=err,
                ))
        else:
            scenario.steps.append(SimpleNamespace(
                step_type="when",
                name="I do something",
                status=status,
                error_message=None,
            ))
        return scenario

    def test_passed_scenario(self):
        scenario = self._make_scenario(status="passed")
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.status_id, TESTRAIL_PASSED)
        self.assertIn("All steps are good", result.comment)

    def test_failed_scenario(self):
        scenario = self._make_scenario(
            status="failed",
            steps=[
                ("passed", "given something", None),
                ("failed", "when I break it", "AssertionError: expected 200 got 500"),
            ]
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.status_id, TESTRAIL_FAILED)
        self.assertIn("when I break it", result.comment)
        self.assertIn("AssertionError", result.comment)

    def test_skipped_and_untested_steps_not_reported_as_failed(self):
        """Behave marks steps after a failure 'skipped' and steps never run
        'untested' — neither is a failure and neither may bury the real error."""
        scenario = self._make_scenario(
            status="failed",
            steps=[
                ("passed", "given something", None),
                ("failed", "when I break it", "AssertionError: expected 200 got 500"),
                ("skipped", "then never reached", None),
                ("untested", "and also never reached", None),
            ]
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertIn("AssertionError: expected 200 got 500", result.comment)
        self.assertNotIn("never reached", result.comment)
        self.assertNotIn("no error message", result.comment)

    def test_hook_failure_message_included_in_comment(self):
        """before_scenario hook error: all steps untested, the cause lives in
        scenario.error_message — it must reach the TestRail comment."""
        scenario = self._make_scenario(
            status="failed",
            error_message="HOOK-ERROR in before_scenario: RuntimeError: browser crashed",
            steps=[
                ("untested", "given a step that never ran", None),
                ("untested", "when another that never ran", None),
            ]
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.status_id, TESTRAIL_FAILED)
        self.assertIn("HOOK-ERROR in before_scenario", result.comment)
        self.assertIn("browser crashed", result.comment)
        self.assertNotIn("never ran", result.comment)
        self.assertNotIn("All steps are good", result.comment)

    def test_undefined_step_labelled_in_comment(self):
        scenario = self._make_scenario(
            status="failed",
            steps=[
                ("passed", "given something", None),
                ("undefined", "when a step nobody defined", None),
            ]
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertIn("when a step nobody defined", result.comment)
        self.assertIn("undefined step — no matching step definition found", result.comment)

    def test_failed_scenario_without_any_error_never_says_all_good(self):
        scenario = self._make_scenario(
            status="failed",
            steps=[("untested", "given a step", None)],
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.status_id, TESTRAIL_FAILED)
        self.assertNotIn("All steps are good", result.comment)
        self.assertIn("no step error was recorded", result.comment)

    def test_step_and_hook_errors_both_included(self):
        scenario = self._make_scenario(
            status="failed",
            error_message="HOOK-ERROR in after_step: cleanup exploded",
            steps=[("failed", "when I break it", "AssertionError: boom")],
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertIn("AssertionError: boom", result.comment)
        self.assertIn("cleanup exploded", result.comment)

    def test_huge_error_message_truncated(self):
        dom_dump = "TimeoutError: waiting for locator\n" + ("<div>" * 2000)
        scenario = self._make_scenario(
            status="failed",
            steps=[("failed", "when the page hangs", dom_dump)],
        )
        result = from_behave_scenario(scenario, "448337")
        self.assertIn("TimeoutError", result.comment)
        self.assertIn("[truncated]", result.comment)
        self.assertLess(len(result.comment), 2500)

    def test_elapsed_set(self):
        scenario = self._make_scenario()
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.elapsed, "3s")

    def test_scenario_name_preserved(self):
        scenario = self._make_scenario(name="Register vehicle smoke")
        result = from_behave_scenario(scenario, "448337")
        self.assertEqual(result.scenario_name, "Register vehicle smoke")


class TestExtractCaseIds(unittest.TestCase):
    def test_single_tag(self):
        scenario = SimpleNamespace(tags=["testrail_C448337", "smoke"])
        self.assertEqual(extract_case_ids(scenario), ["448337"])

    def test_multiple_tags(self):
        scenario = SimpleNamespace(tags=["testrail_C448337", "testrail_C448338"])
        self.assertEqual(extract_case_ids(scenario), ["448337", "448338"])

    def test_no_testrail_tag(self):
        scenario = SimpleNamespace(tags=["smoke", "regression"])
        self.assertEqual(extract_case_ids(scenario), [])

    def test_no_tags_attribute(self):
        scenario = SimpleNamespace()
        self.assertEqual(extract_case_ids(scenario), [])


# ===========================================================================
# 2 — PendingStore lifecycle
# ===========================================================================


class TestPendingStore(unittest.TestCase):
    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        store_path = Path(self._tmpdir.name) / "pending.json"
        self.store = PendingStore(path=store_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_result(self, case_id="448337", status_id=1):
        return TestRailResult(
            case_id=case_id,
            status_id=status_id,
            comment="test comment",
            scenario_name="My scenario",
        )

    def test_add_creates_pending_entry(self):
        self.store.add(self._make_result())
        pending = self.store.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["case_id"], "448337")
        self.assertEqual(pending[0]["status"], STATUS_PENDING)

    def test_get_pending_excludes_pushed(self):
        self.store.add(self._make_result("448337", 1))
        self.store.add(self._make_result("448338", 5))
        self.store.mark_pushed(["448337"])
        pending = self.store.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["case_id"], "448338")

    def test_mark_pushed_all_case_ids(self):
        self.store.add(self._make_result("448337"))
        self.store.add(self._make_result("448338"))
        self.store.mark_pushed(["448337", "448338"])
        self.assertEqual(self.store.get_pending(), [])
        all_entries = self.store.get_all()
        self.assertTrue(all(e["status"] == STATUS_PUSHED for e in all_entries))

    def test_clear_empties_store(self):
        self.store.add(self._make_result())
        self.store.clear()
        self.assertEqual(self.store.get_all(), [])
        self.assertEqual(self.store.get_pending(), [])

    def test_get_status_returns_counts(self):
        self.store.add(self._make_result("448337"))
        self.store.add(self._make_result("448338"))
        self.store.mark_pushed(["448337"])
        status = self.store.get_status()
        self.assertEqual(status["pending_count"], 1)
        self.assertEqual(status["pushed_count"], 1)

    def test_corrupt_json_returns_empty(self):
        """Store should recover gracefully from a corrupt file."""
        self.store._path.write_text("{{ INVALID JSON {{", encoding="utf-8")
        self.assertEqual(self.store.get_all(), [])

    def test_missing_file_returns_empty(self):
        """Store should work even if file doesn't exist yet."""
        store2 = PendingStore(path=Path(self._tmpdir.name) / "nonexistent.json")
        self.assertEqual(store2.get_all(), [])

    def test_thread_safety(self):
        """Concurrent adds should not corrupt the file."""
        results = [self._make_result(str(i)) for i in range(20)]
        threads = [threading.Thread(target=self.store.add, args=(r,)) for r in results]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        all_entries = self.store.get_all()
        self.assertEqual(len(all_entries), 20)

# ===========================================================================
# 3 — TestRailClient HTTP shape
# ===========================================================================


class TestTestRailClientFromEnv(unittest.TestCase):
    def test_raises_when_vars_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove all TESTRAIL_ vars
            for k in ["TESTRAIL_URL", "TESTRAIL_USER", "TESTRAIL_API_KEY"]:
                os.environ.pop(k, None)
            with self.assertRaises(TestRailConfigError):
                TestRailClient.from_env()

    def test_constructs_when_vars_present(self):
        env = {
            "TESTRAIL_URL": "https://test.testrail.io",
            "TESTRAIL_USER": "user@example.com",
            "TESTRAIL_API_KEY": "secret",
        }
        with patch.dict(os.environ, env):
            client = TestRailClient.from_env()
            self.assertEqual(client._base_url, "https://test.testrail.io")

    def test_trailing_slash_stripped(self):
        client = TestRailClient(
            base_url="https://test.testrail.io/",
            user="u",
            api_key="k",
        )
        self.assertFalse(client._base_url.endswith("/"))

    def test_default_run_id_from_env(self):
        with patch.dict(os.environ, {"TESTRAIL_RUN_ID": "42"}):
            self.assertEqual(TestRailClient.default_run_id(), 42)

    def test_default_run_id_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TESTRAIL_RUN_ID", None)
            self.assertIsNone(TestRailClient.default_run_id())


class TestTestRailClientHTTP(unittest.TestCase):
    def setUp(self):
        self.client = TestRailClient(
            base_url="https://test.testrail.io",
            user="user@example.com",
            api_key="test-key",
        )

    def tearDown(self):
        self.client.close()

    def test_add_results_calls_correct_url(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}

        with patch.object(self.client._session, "post", return_value=mock_resp) as mock_post:
            self.client.add_results_for_cases(run_id=123, results=[{"case_id": 448337}])
            url = mock_post.call_args[0][0]
            self.assertIn("/api/v2/add_results_for_cases/123", url)

    def test_add_results_payload_wraps_in_results_key(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}

        with patch.object(self.client._session, "post", return_value=mock_resp) as mock_post:
            self.client.add_results_for_cases(run_id=1, results=[{"case_id": 1, "status_id": 1}])
            payload = mock_post.call_args[1]["json"]
            self.assertIn("results", payload)
            self.assertEqual(payload["results"][0]["case_id"], 1)

    def test_non_200_raises_testrail_api_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with patch.object(self.client._session, "post", return_value=mock_resp):
            with self.assertRaises(TestRailAPIError) as ctx:
                self.client.add_results_for_cases(run_id=1, results=[])
            self.assertEqual(ctx.exception.status_code, 403)

    def test_context_manager(self):
        with TestRailClient("https://t.io", "u", "k") as client:
            self.assertIsNotNone(client)
        # close() should not raise

    def test_add_case_calls_correct_url_with_title_and_steps(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 900001}

        with patch.object(self.client._session, "post", return_value=mock_resp) as mock_post:
            result = self.client.add_case(
                section_id=42, title="Successful registration", custom_steps="When x\nThen y"
            )
            url = mock_post.call_args[0][0]
            payload = mock_post.call_args[1]["json"]
            self.assertIn("/api/v2/add_case/42", url)
            self.assertEqual(payload["title"], "Successful registration")
            self.assertEqual(payload["custom_steps"], "When x\nThen y")
            self.assertEqual(result["id"], 900001)

    def test_add_case_omits_custom_steps_when_none(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 1}

        with patch.object(self.client._session, "post", return_value=mock_resp) as mock_post:
            self.client.add_case(section_id=42, title="T")
            payload = mock_post.call_args[1]["json"]
            self.assertNotIn("custom_steps", payload)

    def _resp(self, status_code=200, json_body=None, text=""):
        resp = MagicMock()
        resp.ok = status_code < 400
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = json_body or {}
        return resp

    def test_add_case_falls_back_to_separated_steps_on_400(self):
        """Text-template field rejected → whole Gherkin body goes into the
        first step of custom_steps_separated (Steps template)."""
        responses = [
            self._resp(400, text="Field :custom_steps is not a valid field"),
            self._resp(200, {"id": 900002}),
        ]
        with patch.object(
            self.client._session, "post", side_effect=responses
        ) as mock_post:
            result = self.client.add_case(
                section_id=42, title="T", custom_steps="When x\nThen y"
            )
            self.assertEqual(mock_post.call_count, 2)
            retry_payload = mock_post.call_args_list[1][1]["json"]
            self.assertEqual(
                retry_payload,
                {"title": "T",
                 "custom_steps_separated": [{"content": "When x\nThen y"}]},
            )
            self.assertEqual(result["id"], 900002)

    def test_add_case_falls_back_to_title_only_when_both_step_formats_rejected(self):
        responses = [
            self._resp(400, text="Field :custom_steps is not a valid field"),
            self._resp(400, text="Field :custom_steps_separated is not a valid field"),
            self._resp(200, {"id": 900003}),
        ]
        with patch.object(
            self.client._session, "post", side_effect=responses
        ) as mock_post:
            result = self.client.add_case(section_id=42, title="T", custom_steps="When x")
            self.assertEqual(mock_post.call_count, 3)
            final_payload = mock_post.call_args_list[2][1]["json"]
            self.assertEqual(final_payload, {"title": "T"})
            self.assertEqual(result["id"], 900003)

    def test_add_case_raises_when_all_payload_variants_rejected(self):
        responses = [self._resp(400, text="bad")] * 3
        with patch.object(
            self.client._session, "post", side_effect=responses
        ) as mock_post:
            with self.assertRaises(TestRailAPIError) as ctx:
                self.client.add_case(section_id=42, title="T", custom_steps="When x")
            self.assertEqual(mock_post.call_count, 3)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_add_case_non_400_error_raises_immediately_without_fallback(self):
        """Auth/permission failures must not burn retries on payload variants."""
        with patch.object(
            self.client._session, "post",
            return_value=self._resp(403, text="No access to project"),
        ) as mock_post:
            with self.assertRaises(TestRailAPIError) as ctx:
                self.client.add_case(section_id=42, title="T", custom_steps="When x")
            self.assertEqual(mock_post.call_count, 1)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_add_case_400_without_steps_raises(self):
        bad_resp = MagicMock()
        bad_resp.ok = False
        bad_resp.status_code = 400
        bad_resp.text = "No access to section"

        with patch.object(self.client._session, "post", return_value=bad_resp):
            with self.assertRaises(TestRailAPIError) as ctx:
                self.client.add_case(section_id=42, title="T")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_default_section_id_from_env(self):
        with patch.dict(os.environ, {"TESTRAIL_SECTION_ID": "77"}):
            self.assertEqual(TestRailClient.default_section_id(), 77)

    def test_default_section_id_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TESTRAIL_SECTION_ID", None)
            self.assertIsNone(TestRailClient.default_section_id())


# ===========================================================================
# 4 — Command parser: !testrail recognised
# ===========================================================================


class TestCommandParserTestrail(unittest.TestCase):
    def test_status_parsed(self):
        cmd = parse_command("!testrail status")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "testrail")
        self.assertEqual(cmd["subcommand"], "status")

    def test_push_parsed(self):
        cmd = parse_command("!testrail push --run-id 42")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "testrail")
        self.assertEqual(cmd["subcommand"], "push")
        self.assertIn("42", cmd["raw"])

    def test_non_testrail_command_not_matched(self):
        cmd = parse_command("!testrail_status")
        self.assertIsNone(cmd)

    def test_raw_text_preserved(self):
        cmd = parse_command("!testrail push --case C448337")
        self.assertEqual(cmd["raw"], "!testrail push --case C448337")


class TestParsePushArgs(unittest.TestCase):
    def test_run_id_parsed(self):
        run_id, cases = _parse_push_args(["--run-id", "42"])
        self.assertEqual(run_id, 42)
        self.assertEqual(cases, [])

    def test_case_parsed(self):
        run_id, cases = _parse_push_args(["--case", "448337"])
        self.assertIsNone(run_id)
        self.assertEqual(cases, ["448337"])

    def test_case_with_c_prefix_stripped(self):
        _, cases = _parse_push_args(["--case", "C448337"])
        self.assertEqual(cases, ["448337"])

    def test_multiple_cases(self):
        _, cases = _parse_push_args(["--case", "448337", "--case", "448338"])
        self.assertIn("448337", cases)
        self.assertIn("448338", cases)

    def test_combined_run_id_and_case(self):
        run_id, cases = _parse_push_args(["--run-id", "99", "--case", "1"])
        self.assertEqual(run_id, 99)
        self.assertEqual(cases, ["1"])


# ===========================================================================
# 5 — Command handler output strings
# ===========================================================================


class TestTestrailCommandHandler(unittest.TestCase):
    def setUp(self):
        # Use a temp store to avoid touching reports/
        self._tmpdir = TemporaryDirectory()
        store_path = Path(self._tmpdir.name) / "pending.json"
        self._store = PendingStore(path=store_path)
        self._patcher = patch(
            "agent.integrations.testrail_command.get_default_store",
            return_value=self._store,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _add_result(self, case_id="448337", status_id=1):
        result = TestRailResult(
            case_id=case_id, status_id=status_id,
            comment="test", scenario_name="My Scenario",
        )
        self._store.add(result)

    # ── status ──────────────────────────────────────────────────────────────

    def test_status_empty_queue(self):
        resp = handle_testrail_command("!testrail status")
        self.assertIn("empty", resp.lower())

    def test_status_shows_pending_count(self):
        self._add_result()
        self._add_result("448338")
        resp = handle_testrail_command("!testrail status")
        self.assertIn("2", resp)

    # ── preview ─────────────────────────────────────────────────────────────

    def test_preview_empty_queue(self):
        resp = handle_testrail_command("!testrail preview")
        self.assertIn("no pending", resp.lower())

    def test_preview_shows_case_id(self):
        self._add_result("448337")
        resp = handle_testrail_command("!testrail preview")
        self.assertIn("C448337", resp)

    # ── discard ─────────────────────────────────────────────────────────────

    def test_discard_clears_queue(self):
        self._add_result()
        resp = handle_testrail_command("!testrail discard")
        self.assertIn("1", resp)
        self.assertEqual(self._store.get_pending(), [])

    def test_discard_empty_queue(self):
        resp = handle_testrail_command("!testrail discard")
        self.assertIn("already empty", resp.lower())

    # ── push ─────────────────────────────────────────────────────────────────

    def test_push_empty_queue(self):
        resp = handle_testrail_command("!testrail push")
        self.assertIn("nothing to push", resp.lower())

    def test_push_sends_to_testrail(self):
        self._add_result("448337", 1)
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("agent.integrations.testrail_command.TestRailClient.from_env", return_value=mock_client), \
             patch("agent.integrations.testrail_command.TestRailClient.default_run_id", return_value=42):
            resp = handle_testrail_command("!testrail push")

        mock_client.add_results_for_cases.assert_called_once()
        call_kwargs = mock_client.add_results_for_cases.call_args[1]
        self.assertEqual(call_kwargs["run_id"], 42)
        self.assertIn("✅", resp)

    def test_push_marks_entries_as_pushed(self):
        self._add_result("448337")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("agent.integrations.testrail_command.TestRailClient.from_env", return_value=mock_client), \
             patch("agent.integrations.testrail_command.TestRailClient.default_run_id", return_value=1):
            handle_testrail_command("!testrail push")

        self.assertEqual(self._store.get_pending(), [])

    def test_push_missing_config(self):
        self._add_result()
        with patch("agent.integrations.testrail_command.TestRailClient.from_env",
                   side_effect=TestRailConfigError("No URL")):
            resp = handle_testrail_command("!testrail push")
        self.assertIn("not configured", resp.lower())

    def test_push_api_error(self):
        self._add_result()
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.add_results_for_cases.side_effect = TestRailAPIError(
            status_code=500, context="test", body="Server Error"
        )
        with patch("agent.integrations.testrail_command.TestRailClient.from_env", return_value=mock_client), \
             patch("agent.integrations.testrail_command.TestRailClient.default_run_id", return_value=1):
            resp = handle_testrail_command("!testrail push")
        self.assertIn("❌", resp)
        self.assertIn("500", resp)

    def test_push_case_filter(self):
        self._add_result("448337", 1)
        self._add_result("448338", 5)
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("agent.integrations.testrail_command.TestRailClient.from_env", return_value=mock_client), \
             patch("agent.integrations.testrail_command.TestRailClient.default_run_id", return_value=1):
            handle_testrail_command("!testrail push --case 448337")

        results = mock_client.add_results_for_cases.call_args[1]["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["case_id"], 448337)

    # ── unknown sub-command → help text ─────────────────────────────────────

    def test_unknown_subcommand_returns_help(self):
        resp = handle_testrail_command("!testrail foobar")
        self.assertIn("!testrail status", resp)

    def test_non_testrail_command(self):
        resp = handle_testrail_command("!run --tags @smoke")
        self.assertIn("Not a !testrail command", resp)


# ===========================================================================
# Test runner
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
