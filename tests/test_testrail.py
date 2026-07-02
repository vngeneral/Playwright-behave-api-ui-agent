"""
TestRail Integration — Unit Tests
===================================
Coverage areas:
  1. Groovy parity — result_mapper logic matches the original JMeter sampler
  2. PendingStore   — lifecycle: add → get_pending → mark_pushed → clear
  3. TestRailClient — HTTP request shape and error handling (no live call)
  4. Command parser — !testrail commands recognised and routed correctly
  5. Command handler— testrail_command.py output strings for each sub-command

Run:
    python -m pytest tests/test_testrail.py -v
"""
from __future__ import annotations

import json
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

from agent.testrail.result_mapper import (
    TESTRAIL_FAILED,
    TESTRAIL_PASSED,
    StepResult,
    TestRailResult,
    build_step_comment,
    extract_case_ids,
    find_failed_steps,
    from_behave_scenario,
    from_step_results,
    is_passing_status_code,
)
from agent.testrail.pending_store import (
    STATUS_PENDING,
    STATUS_PUSHED,
    PendingStore,
)
from agent.testrail.client import (
    TestRailAPIError,
    TestRailClient,
    TestRailConfigError,
)
from agent.integrations.command_parser import parse_command
from agent.integrations.testrail_command import (
    handle_testrail_command,
    _parse_push_args,
)


# ===========================================================================
# 1 — Groovy parity: result_mapper
# ===========================================================================


class TestIsPassingStatusCode(unittest.TestCase):
    """Groovy: code == 200 or code == 204 → pass; anything else → fail."""

    def test_200_passes(self):
        self.assertTrue(is_passing_status_code("200"))

    def test_204_passes(self):
        self.assertTrue(is_passing_status_code("204"))

    def test_201_fails(self):
        self.assertFalse(is_passing_status_code("201"))

    def test_400_fails(self):
        self.assertFalse(is_passing_status_code("400"))

    def test_500_fails(self):
        self.assertFalse(is_passing_status_code("500"))

    def test_non_numeric_fails(self):
        # Groovy: non-numeric statusCode → treated as failure
        self.assertFalse(is_passing_status_code("N/A"))
        self.assertFalse(is_passing_status_code(""))
        self.assertFalse(is_passing_status_code(None))   # type: ignore[arg-type]

    def test_integer_input_passes(self):
        # Should also work when caller passes int instead of str
        self.assertTrue(is_passing_status_code(200))     # type: ignore[arg-type]


class TestFindFailedSteps(unittest.TestCase):
    def test_all_passing_returns_empty(self):
        steps = [
            StepResult("step1", "200"),
            StepResult("step2", "204"),
        ]
        self.assertEqual(find_failed_steps(steps), [])

    def test_one_failed_returned(self):
        steps = [
            StepResult("step1", "200"),
            StepResult("step2", "500", response_data="Internal Server Error"),
        ]
        failed = find_failed_steps(steps)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].name, "step2")

    def test_non_numeric_returned_as_failed(self):
        steps = [StepResult("step1", "N/A")]
        self.assertEqual(len(find_failed_steps(steps)), 1)


class TestBuildStepComment(unittest.TestCase):
    """Mirrors the Groovy comment-building logic."""

    def test_all_pass_returns_passed_status(self):
        steps = [StepResult("Register vehicle", "200", request_data="account:HBL4BP-006")]
        comment, status_id = build_step_comment(steps)
        self.assertEqual(status_id, TESTRAIL_PASSED)
        self.assertIn("All steps are good", comment)

    def test_failed_step_returns_failed_status(self):
        steps = [
            StepResult("Register vehicle", "200"),
            StepResult("Get status", "500", response_data="Server Error"),
        ]
        comment, status_id = build_step_comment(steps)
        self.assertEqual(status_id, TESTRAIL_FAILED)
        self.assertIn("Get status", comment)
        self.assertIn("500", comment)
        self.assertIn("Server Error", comment)

    def test_account_context_in_failed_comment(self):
        steps = [StepResult("step", "404")]
        comment, _ = build_step_comment(steps, account_context="acct:HBL4BP-006")
        self.assertIn("acct:HBL4BP-006", comment)

    def test_non_numeric_status_code_message(self):
        steps = [StepResult("step", "N/A", response_data="connection refused")]
        comment, status_id = build_step_comment(steps)
        self.assertEqual(status_id, TESTRAIL_FAILED)
        self.assertIn("non-numeric", comment)
        self.assertIn("N/A", comment)

    def test_empty_steps_returns_passed(self):
        comment, status_id = build_step_comment([])
        self.assertEqual(status_id, TESTRAIL_PASSED)


class TestFromStepResults(unittest.TestCase):
    def test_all_pass_produces_passed_result(self):
        steps = [StepResult("Register", "200"), StepResult("Check", "204")]
        result = from_step_results("448337", steps, scenario_name="My scenario")
        self.assertEqual(result.case_id, "448337")
        self.assertEqual(result.status_id, TESTRAIL_PASSED)
        self.assertEqual(result.scenario_name, "My scenario")

    def test_one_fail_produces_failed_result(self):
        steps = [StepResult("Register", "200"), StepResult("Check", "503")]
        result = from_step_results("448337", steps)
        self.assertEqual(result.status_id, TESTRAIL_FAILED)

    def test_step_details_populated(self):
        steps = [StepResult("Register", "200", request_data="body", response_data="ok")]
        result = from_step_results("448337", steps)
        self.assertEqual(len(result.step_details), 1)
        self.assertTrue(result.step_details[0]["passed"])

    def test_to_api_dict_shape(self):
        result = TestRailResult(case_id="448337", status_id=1, comment="ok", elapsed="5s")
        d = result.to_api_dict()
        self.assertEqual(d["case_id"], 448337)
        self.assertEqual(d["status_id"], 1)
        self.assertIn("elapsed", d)

    def test_to_api_dict_no_elapsed(self):
        result = TestRailResult(case_id="448337", status_id=1, comment="ok")
        d = result.to_api_dict()
        self.assertNotIn("elapsed", d)


class TestFromBehaveScenario(unittest.TestCase):
    """from_behave_scenario() — maps a Behave-style scenario to TestRailResult."""

    def _make_scenario(self, status="passed", steps=None, name="My Scenario", tags=None):
        scenario = SimpleNamespace(
            name=name,
            status=status,
            tags=tags or [],
            duration=3.5,
            steps=[],
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

    def test_mark_all_pushed(self):
        self.store.add(self._make_result("448337"))
        self.store.add(self._make_result("448338"))
        self.store.mark_all_pushed()
        self.assertEqual(self.store.get_pending(), [])
        all_entries = self.store.get_all()
        self.assertTrue(all(e["status"] == STATUS_PUSHED for e in all_entries))

    def test_clear_empties_store(self):
        self.store.add(self._make_result())
        self.store.clear()
        self.assertEqual(self.store.get_all(), [])
        self.assertFalse(self.store.has_pending())

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

    def test_has_pending_true(self):
        self.store.add(self._make_result())
        self.assertTrue(self.store.has_pending())

    def test_has_pending_false_when_empty(self):
        self.assertFalse(self.store.has_pending())


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

    def test_get_run_calls_correct_url(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 123}

        with patch.object(self.client._session, "get", return_value=mock_resp) as mock_get:
            self.client.get_run(run_id=123)
            url = mock_get.call_args[0][0]
            self.assertIn("/api/v2/get_run/123", url)

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
        self.assertFalse(self._store.has_pending())

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

        self.assertFalse(self._store.has_pending())

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
