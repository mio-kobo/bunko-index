"""The final "Package tests" result must not turn a skipped matrix into a pass."""

import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest

INDEX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INDEX / "tools"))
import bunko_aggregate as aggregate  # noqa: E402

CASES = (
    # name, plan, test, tooling, has_packages, expected
    ("empty matrix, test skipped", "success", "skipped", "success", "false", True),
    ("nonempty matrix, tests succeeded", "success", "success", "success", "true", True),
    ("nonempty matrix, tests skipped", "success", "skipped", "success", "true", False),
    ("nonempty matrix, tests failed", "success", "failure", "success", "true", False),
    ("nonempty matrix, tests cancelled", "success", "cancelled", "success", "true", False),
    ("empty matrix, tests succeeded", "success", "success", "success", "false", False),
    ("empty matrix, tests failed", "success", "failure", "success", "false", False),
    ("missing has_packages", "success", "skipped", "success", "", False),
    ("invalid has_packages", "success", "skipped", "success", "yes", False),
    ("capitalised has_packages", "success", "success", "success", "True", False),
    ("planning failed", "failure", "skipped", "success", "false", False),
    ("planning cancelled", "cancelled", "skipped", "success", "", False),
    ("planning skipped", "skipped", "skipped", "success", "false", False),
    ("tooling failed", "success", "skipped", "failure", "false", False),
    ("tooling cancelled", "success", "success", "cancelled", "true", False),
    ("tooling skipped", "success", "skipped", "skipped", "false", False),
    ("unknown test state", "success", "neutral", "success", "true", False),
    ("missing test state with packages", "success", "", "success", "true", False),
    ("missing test state without packages", "success", "", "success", "false", False),
)


class AggregateTests(unittest.TestCase):
    def test_verdict_table(self):
        for name, plan, test, tooling, has_packages, expected in CASES:
            with self.subTest(name):
                self.assertEqual(aggregate.verdict(plan, test, tooling, has_packages)[0], expected)

    def test_command_exit_status_matches_the_verdict(self):
        for name, plan, test, tooling, has_packages, expected in CASES:
            with self.subTest(name):
                result = subprocess.run(
                    [sys.executable, "-I", str(INDEX / "tools/bunko_aggregate.py"), "--plan", plan, "--test", test,
                     "--tooling", tooling, "--has-packages", has_packages],
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0 if expected else 1, result.stdout)
                self.assertTrue(result.stdout.startswith("PASS" if expected else "FAIL"))

    def test_failure_reasons_name_the_problem(self):
        self.assertIn("require tests", aggregate.verdict("success", "skipped", "success", "true")[1])
        self.assertIn("planning", aggregate.verdict("failure", "skipped", "success", "false")[1])
        self.assertIn("has_packages", aggregate.verdict("success", "skipped", "success", "")[1])

    def test_missing_arguments_fail_instead_of_passing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aggregate.main([]), 1)

    def test_workflow_uses_the_checked_aggregator_with_all_four_inputs(self):
        workflow = (INDEX / ".github/workflows/test-packages.yml").read_text(encoding="utf-8")
        result = workflow[workflow.index("\n  result:"):]
        for expected in ("tools/bunko_aggregate.py", "needs.plan.outputs.has_packages", "needs.plan.result",
                         "needs.test.result", "needs.tooling.result", "if: always()"):
            self.assertIn(expected, result)
        self.assertNotIn('= skipped', result, "the unconditional skipped shortcut must not return")


if __name__ == "__main__":
    unittest.main()
