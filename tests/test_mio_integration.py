"""End-to-end checks against the real, pinned Mio compiler.

Opt in with BUNKO_MIO_INTEGRATION=1. The compiler is downloaded from the
official mio-kobo/mio release and verified against policies/platform-targets.toml.
Package releases are simulated, so no network access to publisher repositories
is needed.
"""

import os
from pathlib import Path
import platform
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bunko_ci as helpers  # noqa: E402

ci, source, validate = helpers.ci, helpers.source, helpers.validate

HOST = {("Linux", "x86_64"): "linux/x86_64", ("Linux", "AMD64"): "linux/x86_64",
        ("Darwin", "arm64"): "macos/arm64", ("Windows", "AMD64"): "windows/x86_64"}.get(
            (platform.system(), platform.machine()))
ENABLED = os.environ.get("BUNKO_MIO_INTEGRATION") == "1"

LIBRARY = b"koukai waza answer() -> int64 { kaesu 42; }\n"
USES_LIBRARY = b'yobu "kiso/testing";\nyobu "alice/json";\nwaza test_answer() -> unit { testing.expect(json.answer() == 42, "answer"); }\n'
FAILING = b'yobu "kiso/testing";\nwaza test_bad() -> unit { testing.expect(1 == 2, "one is not two"); }\n'
BROKEN = b'yobu "kiso/testing";\nwaza test_x( -> unit {\n'


class RealCompilerFetcher(helpers.FakeFetcher):
    """Simulated GitHub releases for packages; the real network for the compiler only."""

    real = source.Fetcher()

    def download(self, url, destination, max_bytes):
        if url.startswith("https://github.com/mio-kobo/mio/releases/download/"):
            return self.real.download(url, destination, max_bytes)
        return super().download(url, destination, max_bytes)


@unittest.skipUnless(ENABLED and HOST, "set BUNKO_MIO_INTEGRATION=1 on a supported V1 target")
class RealMioTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = RealCompilerFetcher()
        self.index = helpers.Index(self, self.fetcher)
        self.count = 0

    def execute(self, path):
        self.count += 1
        work = self.index.scratch / f"work{self.count}"
        ref = self.index.commit("add")
        ci.prepare(self.index.root, ref, path, HOST, work, self.fetcher)
        manifest, ok, passed, failed, lines, stderr = ci.run(work)
        return manifest, ok, passed, failed, lines

    def test_passing_package_with_an_exact_dependency(self):
        self.index.add("alice/json", files={"json.mio": LIBRARY, "json_test.mio": helpers.PASSING_TEST.encode()})
        path = self.index.add("bob/app", files={"app_test.mio": USES_LIBRARY}, deps={"alice/json": "1.0.0"})
        manifest, ok, passed, failed, lines = self.execute(path)
        self.assertEqual((ok, passed, failed, lines), (True, 1, 0, []))
        self.assertEqual(manifest["mio_version"], "2.1.0")

    def test_failing_test_fails_the_job_with_the_assertion_message(self):
        path = self.index.add("alice/json", files={"json.mio": LIBRARY, "json_test.mio": FAILING})
        _, ok, passed, failed, lines = self.execute(path)
        self.assertFalse(ok)
        self.assertEqual((passed, failed), (0, 1))
        self.assertTrue(any("test_bad" in line and "one is not two" in line for line in lines), lines)

    def test_one_failure_among_passing_tests_still_fails(self):
        path = self.index.add("alice/json", files={
            "json.mio": LIBRARY, "a_test.mio": helpers.PASSING_TEST.encode(), "sub/b_test.mio": FAILING})
        _, ok, passed, failed, _ = self.execute(path)
        self.assertFalse(ok)
        self.assertEqual((passed, failed), (1, 1))

    def test_compile_error_fails_the_job(self):
        path = self.index.add("alice/json", files={"json.mio": LIBRARY, "json_test.mio": BROKEN})
        _, ok, _, failed, lines = self.execute(path)
        self.assertFalse(ok)
        self.assertGreaterEqual(failed, 1)
        self.assertTrue(any("compile error" in line for line in lines), lines)

    def test_a_test_file_that_declares_no_test_functions_is_not_enough(self):
        path = self.index.add("alice/json", files={"json.mio": LIBRARY, "json_test.mio": b'yobu "kiso/testing";\n'})
        _, ok, _, _, lines = self.execute(path)
        self.assertFalse(ok)
        self.assertTrue(any("no tests" in line for line in lines), lines)

    def test_missing_dependency_files_surface_as_a_compile_error(self):
        # alice/json is in the closure but its source lacks the imported function.
        self.index.add("alice/json", files={"json.mio": b"// empty\n", "json_test.mio": helpers.PASSING_TEST.encode()})
        path = self.index.add("bob/app", files={"app_test.mio": USES_LIBRARY}, deps={"alice/json": "1.0.0"})
        _, ok, *_ = self.execute(path)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
