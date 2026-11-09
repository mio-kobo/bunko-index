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
PLATFORMS = '[platforms]\nlinux = ["x86_64"]\nmacos = ["arm64"]\nwindows = ["x86_64"]\n'
ALL = {"linux": ["x86_64"], "macos": ["arm64"], "windows": ["x86_64"]}


class IndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ("tools", "schemas", "policies"):
            shutil.copytree(INDEX / directory, self.root / directory)

    def package(self, name="alice/json", version="1.0.0", *, path=None, publisher=None, deps=None,
                platforms=None):
        namespace, package = name.split("/", 1)
        publisher = publisher or namespace
        doc = (FIXTURE.replace('name = "alice/json"', f"name = {json.dumps(name)}")
               .replace('version = "1.0.0"', f"version = {json.dumps(version)}")
               .replace('publisher = "github:alice"', f'publisher = "github:{publisher}"')
               .replace("github.com/alice/json", f"github.com/{publisher}/{package}")
               .replace("github.com/repos/alice/json", f"github.com/repos/{publisher}/{package}"))
        if platforms is not None:
            block = "".join(f"{os_name} = {json.dumps(archs)}\n" for os_name, archs in platforms.items())
            doc = doc.replace(PLATFORMS, "[platforms]\n" + block)
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

    def test_platforms_are_required_and_nonempty(self):
        for label, text in (("missing", ""), ("empty", "[platforms]\n")):
            with self.subTest(case=label):
                record = self.package()
                record.write_text(FIXTURE.replace(PLATFORMS, text), encoding="utf-8")
                output = self.validate(code="BUNKO_E_SCHEMA")
                self.assertIn("platforms", output)

    def test_invalid_platform_declarations(self):
        cases = {
            "unknown os": ('linux = ["x86_64"]\nfreebsd = ["x86_64"]', "is not an allowed field"),
            "darwin alias": ('darwin = ["arm64"]', "is not an allowed field"),
            "win32 alias": ('win32 = ["x86_64"]', "is not an allowed field"),
            "unknown arch": ('linux = ["riscv64"]', "must be one of"),
            "amd64 alias": ('linux = ["amd64"]', "must be one of"),
            "x64 alias": ('windows = ["x64"]', "must be one of"),
            "aarch64 alias": ('linux = ["aarch64"]', "must be one of"),
            "wildcard arch": ('linux = ["*"]', "must be one of"),
            "wildcard os": ('"*" = ["x86_64"]', "is not an allowed field"),
            "empty array": ('linux = []', "must not be empty"),
            "duplicate": ('linux = ["x86_64", "x86_64"]', "duplicate"),
            "string not array": ('linux = "x86_64"', "must be an array"),
            "number member": ('linux = [1]', "must be a string"),
            "uppercase": ('linux = ["X86_64"]', "must be one of"),
        }
        for label, (body, message) in cases.items():
            with self.subTest(case=label):
                record = self.package()
                record.write_text(FIXTURE.replace(PLATFORMS, "[platforms]\n" + body + "\n"), encoding="utf-8")
                self.assertIn(message, self.validate(code="BUNKO_E_SCHEMA"))

    def test_platforms_must_be_a_table_without_extra_nesting(self):
        for text in ('platforms = ["linux"]\n', '[platforms.linux]\narch = "x86_64"\n'):
            with self.subTest(text=text):
                record = self.package()
                body = FIXTURE.replace(PLATFORMS, "")
                body = body.replace("[dependencies]", text + "\n[dependencies]") if text.startswith("[") \
                    else text + body
                record.write_text(body, encoding="utf-8")
                self.validate(code="BUNKO_E_SCHEMA")

    def test_valid_one_and_multi_platform_packages(self):
        self.package("alice/one", platforms={"linux": ["x86_64"]})
        self.package("alice/two", platforms={"macos": ["arm64"], "windows": ["x86_64"]})
        self.package("alice/all", platforms=ALL)
        self.assertIn("Validated 3", self.validate())

    def test_syntactically_valid_targets_without_released_mio_are_rejected(self):
        for os_name, arch in (("linux", "arm64"), ("macos", "x86_64"), ("windows", "arm64")):
            with self.subTest(target=f"{os_name}/{arch}"):
                self.package(platforms={os_name: [arch]})
                output = self.validate(code="BUNKO_E_PLATFORM")
                self.assertIn(f"{os_name}/{arch}", output)

    def test_mio_compatibility_is_independent_of_platforms(self):
        record = self.package(platforms={"linux": ["x86_64"]})
        record.write_text(record.read_text().replace('min = "2.0.0"', 'min = "2.1.0"'))
        self.validate()
        record.write_text(record.read_text().replace('before = "3.0.0"', 'before = "2.1.0"'))
        self.validate(code="BUNKO_E_COMPATIBILITY")

    def test_dependency_closure_must_support_every_declared_target(self):
        self.package("alice/json", platforms={"linux": ["x86_64"]})
        self.package("bob/app", platforms={"linux": ["x86_64"], "windows": ["x86_64"]},
                     deps={"alice/json": "1.0.0"})
        output = self.validate(code="BUNKO_E_PLATFORM_DEPENDENCY")
        self.assertIn("bob/app@1.0.0 -> alice/json@1.0.0 does not declare windows/x86_64", output)
        self.assertIn("required by bob/app@1.0.0", output)
        self.assertIn("declares: linux/x86_64", output)

    def test_transitive_platform_gap_reports_the_full_chain(self):
        self.package("alice/leaf", platforms={"linux": ["x86_64"]})
        self.package("bob/mid", platforms=ALL, deps={"alice/leaf": "1.0.0"})
        self.package("carol/root", platforms={"macos": ["arm64"]}, deps={"bob/mid": "1.0.0"})
        output = self.validate(code="BUNKO_E_PLATFORM_DEPENDENCY")
        self.assertIn("carol/root@1.0.0 -> bob/mid@1.0.0 -> alice/leaf@1.0.0 does not declare macos/arm64", output)
        self.assertEqual(output.count("BUNKO_E_PLATFORM_DEPENDENCY"), 2)  # carol/root's chain, and bob/mid as its own root

    def test_dependencies_may_support_more_targets_than_the_dependent(self):
        self.package("alice/json", platforms=ALL)
        self.package("bob/app", platforms={"macos": ["arm64"]}, deps={"alice/json": "1.0.0"})
        self.validate()

    def test_combined_roots_check_each_roots_own_targets(self):
        self.package("alice/json", platforms={"linux": ["x86_64"]})
        self.package("bob/web", platforms={"linux": ["x86_64"]}, deps={"alice/json": "1.0.0"})
        self.package("carol/api", platforms={"linux": ["x86_64"], "macos": ["arm64"]})
        self.validate("--roots", "bob/web@1.0.0", "carol/api@1.0.0")
        self.package("dan/cli", platforms=ALL, deps={"alice/json": "1.0.0"})
        self.validate("--roots", "bob/web@1.0.0", "dan/cli@1.0.0", code="BUNKO_E_PLATFORM_DEPENDENCY")

    def test_platform_gap_in_an_already_invalid_or_revoked_dependency_keeps_its_own_error(self):
        self.package("alice/json", platforms={"linux": ["x86_64"]})
        self.package("bob/app", platforms=ALL, deps={"alice/json": "1.0.0"})
        policy = self.root / "policies/revoked-versions.toml"
        policy.write_text('[revoked]\n"alice/json@1.0.0" = "https://example.org/advisory/1"\n')
        output = self.validate(code="BUNKO_E_REVOKED")
        self.assertNotIn("PLATFORM_DEPENDENCY", output)
        self.package("carol/app", platforms=ALL, deps={"dan/missing": "1.0.0"})
        self.validate(code="BUNKO_E_MISSING_PACKAGE")

    def test_validator_refuses_schema_keywords_it_does_not_implement(self):
        schema = self.root / "schemas/package.schema.json"
        document = json.loads(schema.read_text())
        document["properties"]["platforms"]["maxProperties"] = 1
        schema.write_text(json.dumps(document))
        self.package()
        output = self.validate(code="BUNKO_E_VALIDATION")
        self.assertIn("unsupported schema keyword", output)

    def test_platform_policy_must_name_complete_targets(self):
        policy = self.root / "policies/platform-targets.toml"
        policy.write_text(policy.read_text().replace('runner = "ubuntu-24.04"\n', "", 1))
        self.package()
        self.validate(code="BUNKO_E_VALIDATION")


if __name__ == "__main__":
    unittest.main()
