"""
Unit tests for integrations/command_parser.py
No network calls needed.
"""
import unittest

from integrations.command_parser import (
    CommandParseError,
    command_to_argv,
    parse_command,
)


class TestParseCommandReturnsNone(unittest.TestCase):
    """Messages that are not commands should return None."""

    def test_plain_text(self):
        self.assertIsNone(parse_command("Hello team"))

    def test_empty_string(self):
        self.assertIsNone(parse_command(""))

    def test_whitespace_only(self):
        self.assertIsNone(parse_command("   "))

    def test_unknown_command(self):
        # !foo is unknown, so silently returns None
        self.assertIsNone(parse_command("!foo --bar"))


class TestHelpCommand(unittest.TestCase):
    def test_help(self):
        result = parse_command("!help")
        self.assertEqual(result, {"action": "help"})

    def test_help_with_trailing_space(self):
        result = parse_command("  !help  ")
        self.assertEqual(result, {"action": "help"})


class TestStatusCommand(unittest.TestCase):
    def test_status(self):
        result = parse_command("!status")
        self.assertEqual(result, {"action": "status"})


class TestRunCommandDefaults(unittest.TestCase):
    def test_bare_run(self):
        result = parse_command("!run")
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["tags"], [])
        self.assertIsNone(result["browser"])
        self.assertIsNone(result["env"])
        self.assertFalse(result["headless"])
        self.assertFalse(result["parallel"])
        self.assertFalse(result["priority"])


class TestRunCommandTags(unittest.TestCase):
    def test_single_tag_with_at(self):
        result = parse_command("!run --tags @smoke")
        self.assertEqual(result["tags"], ["@smoke"])

    def test_single_tag_without_at(self):
        result = parse_command("!run --tags smoke")
        self.assertEqual(result["tags"], ["@smoke"])

    def test_multiple_tags(self):
        result = parse_command("!run --tags @smoke @regression")
        self.assertEqual(result["tags"], ["@smoke", "@regression"])


class TestRunCommandBrowser(unittest.TestCase):
    def test_chromium(self):
        result = parse_command("!run --browser chromium")
        self.assertEqual(result["browser"], "chromium")

    def test_firefox(self):
        result = parse_command("!run --browser firefox")
        self.assertEqual(result["browser"], "firefox")

    def test_webkit(self):
        result = parse_command("!run --browser webkit")
        self.assertEqual(result["browser"], "webkit")

    def test_unknown_browser_raises(self):
        with self.assertRaises(CommandParseError) as ctx:
            parse_command("!run --browser ie11")
        self.assertIn("ie11", str(ctx.exception))

    def test_missing_browser_value_raises(self):
        with self.assertRaises(CommandParseError):
            parse_command("!run --browser")


class TestRunCommandEnv(unittest.TestCase):
    def test_dev(self):
        self.assertEqual(parse_command("!run --env dev")["env"], "dev")

    def test_staging(self):
        self.assertEqual(parse_command("!run --env staging")["env"], "staging")

    def test_prod(self):
        self.assertEqual(parse_command("!run --env prod")["env"], "prod")

    def test_unknown_env_raises(self):
        with self.assertRaises(CommandParseError):
            parse_command("!run --env qa")


class TestRunCommandFlags(unittest.TestCase):
    def test_headless(self):
        self.assertTrue(parse_command("!run --headless")["headless"])

    def test_parallel(self):
        self.assertTrue(parse_command("!run --parallel")["parallel"])

    def test_priority(self):
        self.assertTrue(parse_command("!run --priority")["priority"])


class TestRunCommandCombined(unittest.TestCase):
    def test_full_command(self):
        cmd = parse_command("!run --tags @smoke @regression --browser firefox --env staging --headless --parallel --priority")
        self.assertEqual(cmd["action"], "run")
        self.assertEqual(cmd["tags"], ["@smoke", "@regression"])
        self.assertEqual(cmd["browser"], "firefox")
        self.assertEqual(cmd["env"], "staging")
        self.assertTrue(cmd["headless"])
        self.assertTrue(cmd["parallel"])
        self.assertTrue(cmd["priority"])

    def test_case_insensitive_flags(self):
        result = parse_command("!run --browser CHROMIUM")
        self.assertEqual(result["browser"], "chromium")


class TestCommandToArgv(unittest.TestCase):
    def test_empty_run(self):
        cmd = {"action": "run", "tags": [], "browser": None, "env": None,
               "headless": False, "parallel": False, "priority": False}
        self.assertEqual(command_to_argv(cmd), [])

    def test_tags_and_browser(self):
        cmd = {"action": "run", "tags": ["@smoke"], "browser": "firefox", "env": None,
               "headless": True, "parallel": False, "priority": False}
        argv = command_to_argv(cmd)
        self.assertIn("--tags", argv)
        self.assertIn("@smoke", argv)
        self.assertIn("--browser", argv)
        self.assertIn("firefox", argv)
        self.assertIn("--headless", argv)
        self.assertNotIn("--parallel", argv)

    def test_all_flags(self):
        cmd = {"action": "run", "tags": ["@smoke", "@api"], "browser": "chromium",
               "env": "staging", "headless": True, "parallel": True, "priority": True}
        argv = command_to_argv(cmd)
        self.assertIn("--parallel", argv)
        self.assertIn("--priority", argv)
        self.assertIn("--env", argv)
        env_idx = argv.index("--env")
        self.assertEqual(argv[env_idx + 1], "staging")

    def test_non_run_action_raises(self):
        with self.assertRaises(CommandParseError):
            command_to_argv({"action": "status"})


class TestUnknownOption(unittest.TestCase):
    def test_unknown_flag_raises(self):
        with self.assertRaises(CommandParseError) as ctx:
            parse_command("!run --unknown-flag")
        self.assertIn("--unknown-flag", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
