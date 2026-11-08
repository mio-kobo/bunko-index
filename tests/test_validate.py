import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

INDEX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INDEX / "tools"))
from validate import damerau_levenshtein  # noqa: E402


FIXTURE = (INDEX / "tests/fixtures/package.toml").read_text(encoding="utf-8")


class IndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ("tools", "schemas", "policies"):
            shutil.copytree(INDEX / directory, self.root / directory)

    def package(self, name="alice/json", version="1.0.0", *, path=None, publisher=None, deps=None):
        namespace, package = name.split("/", 1)
        publisher = publisher or namespace
        doc = (FIXTURE.replace('name = "alice/json"', f"name = {json.dumps(name)}")
               .replace('version = "1.0.0"', f"version = {json.dumps(version)}")
               .replace('publisher = "github:alice"', f'publisher = "github:{publisher}"')
               .replace("github.com/alice/json", f"github.com/{publisher}/{package}")
               .replace("github.com/repos/alice/json", f"github.com/repos/{publisher}/{package}"))
        for dependency, exact in (deps or {}).items():
            doc += f"{json.dumps(dependency)} = {json.dumps(exact)}\n"
        record = self.root / (path or f"packages/{name}/{version}.toml")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(doc, encoding="utf-8")
        return record

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, stderr=subprocess.PIPE,
        ).strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Index Tests", "-c", "user.email=test@example.org", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def init(self):
        self.git("init", "-q")
        # The surrounding checkout or user config must not change this test.
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.filemode", "true")
        self.git("config", "core.autocrlf", "false")

    def validate(self, *args, code=None):
        result = subprocess.run(
            [sys.executable, "-I", str(self.root / "tools/validate.py"), *args],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, int(code is not None), output)
        if code:
            self.assertIn(code, output)
        return output

    def test_valid_record_and_exact_dependency(self):
        self.package()
        self.package("bob/app", deps={"alice/json": "1.0.0"})
        self.assertIn("Validated 2", self.validate())

    def test_malformed_toml_and_duplicate_dependency_keys(self):
        record = self.package(deps={"bob/json": "1.0.0"})
        record.write_text(record.read_text() + '"bob/json" = "2.0.0"\n')
        self.validate(code="BUNKO_E_RECORD")

    def test_invalid_metadata(self):
        cases = [
            ('name = "alice/json"', 'name = "Alice/json"'),
            ('name = "alice/json"', 'name = "alice/../json"'),
            ('name = "alice/json"', 'name = "alice/jѕon"'),
            ('version = "1.0.0"', 'version = "01.0.0"'),
            ('license = "MIT"', ''),
            ('license = "MIT"', 'license = 42'),
            ('license = "MIT"', 'license = "MIT"\ninstall = "run.sh"'),
            ('sha256 = "', 'sha256 = "X'),
            ('[dependencies]', '[dependencies]\n"bob/json" = "latest"'),
            ('[dependencies]', '[dependencies]\n"bob/json" = "^1.0.0"'),
            ('[dependencies]', '[dependencies]\n"bob/json" = "~1.0.0"'),
            ('[dependencies]', '[dependencies]\n"bob/json\\n" = "1.0.0"'),
            ('[dependencies]', '[dependencies]\n"alice/../json" = "1.0.0"'),
            ('[dependencies]', '[dependencies]\n"alice/json" = 1.0'),
        ]
        for old, new in cases:
            with self.subTest(value=new):
                record = self.package()
                record.write_text(FIXTURE.replace(old, new), encoding="utf-8")
                self.validate(code="BUNKO_E_SCHEMA")

    def test_source_address_rejects_moving_refs_and_unsafe_urls(self):
        commit = "0123456789abcdef0123456789abcdef01234567"
        for old, new in (
            (commit, "main"),
            ("/assets/789012", "/assets/789012?download=1"),
            ("https://api.github.com", "https://api.github.com@evil.example"),
            ("https://github.com/alice/json", "https://github.com/alice/.."),
        ):
            with self.subTest(url=new):
                record = self.package()
                record.write_text(FIXTURE.replace(old, new))
                self.validate(code="BUNKO_E_SCHEMA" if new != "https://github.com/alice/.." else "BUNKO_E_SOURCE")

    def test_archive_must_belong_to_the_publisher_repository(self):
        record = self.package()
        record.write_text(FIXTURE.replace("repos/alice/json", "repos/bob/json"))
        self.validate(code="BUNKO_E_SOURCE")

    def test_compiler_compatibility_interval(self):
        record = self.package()
        record.write_text(FIXTURE.replace('before = "3.0.0"', 'before = "2.0.0"'))
        self.validate(code="BUNKO_E_COMPATIBILITY")

    def test_path_and_metadata_must_agree(self):
        self.package(path="packages/alice/json/1.0.1.toml")
        self.validate(code="BUNKO_E_PATH")

    def test_duplicate_release_in_another_path(self):
        self.package()
        self.package(path="packages/alice/other/1.0.0.toml")
        self.validate(code="BUNKO_E_DUPLICATE")

    def test_reserved_and_community_impersonation(self):
        self.package("mio/parser")
        self.package("kiso/io")
        self.package("bunko/cli", publisher="mio-kobo")
        self.package("bob/parser", publisher="alice")
        output = self.validate(code="BUNKO_E_IDENTITY")
        self.assertEqual(output.count("BUNKO_E_IDENTITY"), 4)

    def test_reserved_submission_cannot_self_authorize(self):
        self.init()
        base = self.commit("Establish the policy")
        self.package("mio/parser", publisher="mio-kobo")
        policy = self.root / "policies/reserved-names.toml"
        policy.write_text(policy.read_text().replace("[72144156]", "[72144156, 42]"))
        head = self.commit("Propose a package and an allowlist edit")
        self.validate("--base", base, "--candidate", head, "--author-id", "42", code="BUNKO_E_IDENTITY")
        self.validate("--base", base, "--candidate", head, "--author-id", "72144156")

    def test_visual_and_edit_distance_warnings_do_not_reject(self):
        for namespace in ("mi0", "m1o", "mlo", "kis0", "k1so", "ki5o", "mioo", "moi", "kisoo"):
            self.package(f"{namespace}/parser")
        output = self.validate("--github")
        self.assertEqual(output.count("::warning "), 9)
        self.assertEqual(output.count("visually"), 6)
        self.assertIn("edit distance", output)
        self.assertIn("maintainer review required", output)

    def test_similar_package_basename_is_normal(self):
        self.package("alice/mio-tools")
        self.package("alice/mio")
        self.assertNotIn("MANUAL_REVIEW", self.validate("--github"))

    def test_unrestricted_damerau_levenshtein(self):
        self.assertEqual(damerau_levenshtein("moi", "mio"), 1)
        self.assertEqual(damerau_levenshtein("ca", "abc"), 2)
        self.assertEqual(damerau_levenshtein("", "abc"), 3)

    def test_missing_package_and_missing_version(self):
        self.package()
        self.package("bob/app", deps={"alice/json": "2.0.0", "carol/xml": "1.0.0"})
        output = self.validate(code="BUNKO_E_MISSING_PACKAGE")
        self.assertIn("BUNKO_E_MISSING_VERSION", output)

    def test_indirect_invalid_reserved_dependency(self):
        self.package("kiso/io", publisher="alice")
        self.package("bob/mid", deps={"kiso/io": "1.0.0"})
        self.package("alice/root", deps={"bob/mid": "1.0.0"})
        output = self.validate(code="BUNKO_E_INVALID_DEPENDENCY")
        self.assertIn("alice/root@1.0.0 -> bob/mid@1.0.0 -> kiso/io@1.0.0", output)

    def test_cycle_reports_the_full_path(self):
        self.package("alice/a", deps={"bob/b": "1.0.0"})
        self.package("bob/b", deps={"alice/a": "1.0.0"})
        output = self.validate(code="BUNKO_E_CYCLE")
        self.assertIn("alice/a@1.0.0 -> bob/b@1.0.0 -> alice/a@1.0.0", output)

    def test_conflicting_versions_include_both_paths(self):
        self.package("bob/json", "1.2.0")
        self.package("bob/json", "2.0.0")
        self.package("alice/web", deps={"bob/json": "1.2.0"})
        self.package("carol/api", "2.0.0", deps={"bob/json": "2.0.0"})
        output = self.validate("--roots", "alice/web@1.0.0", "carol/api@2.0.0", code="BUNKO_E_VERSION_CONFLICT")
        self.assertIn("alice/web@1.0.0 -> bob/json@1.2.0", output)
        self.assertIn("carol/api@2.0.0 -> bob/json@2.0.0", output)
        self.package("alice/root", deps={"alice/web": "1.0.0", "carol/api": "2.0.0"})
        output = self.validate(code="BUNKO_E_VERSION_CONFLICT")
        self.assertIn("alice/root@1.0.0 -> carol/api@2.0.0 -> bob/json@2.0.0", output)

    def test_diamond_with_same_version_is_valid(self):
        self.package()
        self.package("bob/left", deps={"alice/json": "1.0.0"})
        self.package("bob/right", deps={"alice/json": "1.0.0"})
        self.package("bob/root", deps={"bob/left": "1.0.0", "bob/right": "1.0.0"})
        self.validate()

    def test_revocation_blocks_indirect_use_and_keeps_record(self):
        self.package()
        self.package("bob/mid", deps={"alice/json": "1.0.0"})
        self.package("bob/root", deps={"bob/mid": "1.0.0"})
        policy = self.root / "policies/revoked-versions.toml"
        policy.write_text('[revoked]\n"alice/json@1.0.0" = "https://example.org/advisory/1"\n')
        output = self.validate(code="BUNKO_E_REVOKED")
        self.assertIn("bob/root@1.0.0 -> bob/mid@1.0.0 -> alice/json@1.0.0", output)
        with policy.open("a") as stream:
            for name in ("bob/mid", "bob/root"):
                stream.write(f'"{name}@1.0.0" = "https://example.org/advisory/1"\n')
        self.validate()
        self.validate("--roots", "alice/json@1.0.0", code="BUNKO_E_REVOKED")

    def test_published_records_are_byte_and_mode_immutable(self):
        self.init()
        record = self.package()
        base = self.commit("Published")
        self.package(version="1.0.1")
        self.validate("--base", base)
        record.write_text(FIXTURE + "# Comment edits are changes too.\n")
        self.validate("--base", base, code="BUNKO_E_IMMUTABLE")
        head = self.commit("Alter bytes")
        self.validate("--base", base, "--candidate", head, code="BUNKO_E_IMMUTABLE")
        record.write_text(FIXTURE)
        record.chmod(0o755)
        head = self.commit("Alter file mode")
        self.validate("--base", base, "--candidate", head, code="BUNKO_E_IMMUTABLE")
        record.unlink()
        head = self.commit("Delete a version")
        self.validate("--base", base, "--candidate", head, code="BUNKO_E_IMMUTABLE")

    def test_symlink_and_unexpected_file_are_rejected(self):
        self.init()
        record = self.package()
        outside = self.root / "outside.toml"
        record.rename(outside)
        record.symlink_to(outside)
        (record.parent / "install.sh").write_text("exit 1\n")
        self.validate(code="BUNKO_E_RECORD")
        head = self.commit("Add unsafe entries")
        output = self.validate("--candidate", head, code="BUNKO_E_RECORD")
        self.assertIn("regular non-executable files", output)
        self.assertIn("install.sh", output)

    def test_annotations_escape_newlines_and_properties(self):
        path = "packages/alice/json/bad,:%\n::notice::injected.toml"
        self.package(path=path)
        output = self.validate("--github", code="BUNKO_E_RECORD")
        self.assertIn("bad%2C%3A%25%0A", output)
        self.assertEqual(len(output.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
