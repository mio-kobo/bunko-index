"""Tests for the trusted package-test tooling: archives, provenance, matrix, reports."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

INDEX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INDEX / "tools"))
import bunko_archive as archive  # noqa: E402
import bunko_ci as ci  # noqa: E402
import bunko_source as source  # noqa: E402
import validate  # noqa: E402

PASSING_TEST = 'yobu "kiso/testing";\nwaza test_ok() -> unit { testing.expect(1 == 1, "ok"); }\n'
ALL = {"linux": ["x86_64"], "macos": ["arm64"], "windows": ["x86_64"]}


def tarball(entries):
    """Build a .tar.gz from {name: bytes | (TarInfo-kwargs, bytes)}."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, value in entries.items():
            kwargs, data = value if isinstance(value, tuple) else ({}, value)
            info = tarfile.TarInfo(name)
            info.mode = kwargs.pop("mode", 0o644)
            info.type = kwargs.pop("type", tarfile.REGTYPE)
            info.linkname = kwargs.pop("linkname", "")
            info.size = len(data) if info.type == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return buffer.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def extract(self, entries):
        path = self.dir / "a.tar.gz"
        path.write_bytes(entries if isinstance(entries, bytes) else tarball(entries))
        return archive.extract_package(path, self.dir / "out")

    def reject(self, entries, text):
        with self.assertRaises(archive.ArchiveError) as caught:
            self.extract(entries)
        self.assertIn(text, str(caught.exception))
        self.assertFalse((self.dir / "out").exists(), "nothing may be written for a rejected archive")

    def test_accepts_source_docs_and_license_files(self):
        files = self.extract({"util.mio": b"", "sub/": ({"type": tarfile.DIRTYPE}, b""),
                              "sub/util_test.mio": PASSING_TEST.encode(), "README.md": b"# x\n",
                              "LICENSE": b"MIT\n", "LICENSE-MIT": b"MIT\n", "notes.txt": "雪\n".encode()})
        self.assertEqual(files, sorted(files))
        self.assertIn("sub/util_test.mio", files)
        self.assertTrue((self.dir / "out/sub/util_test.mio").is_file())

    def test_rejects_traversal_and_absolute_paths(self):
        for name in ("../evil.mio", "a/../../evil.mio", "/etc/passwd.mio", "C:/x.mio", "a//b.mio", "./a.mio"):
            with self.subTest(name=name):
                self.reject({name: b""}, "path")

    def test_rejects_symlinks_hardlinks_and_devices(self):
        self.reject({"link.mio": ({"type": tarfile.SYMTYPE, "linkname": "/etc/passwd"}, b"")}, "symlink")
        self.reject({"a.mio": b"", "link.mio": ({"type": tarfile.LNKTYPE, "linkname": "a.mio"}, b"")}, "hard link")
        self.reject({"dev.mio": ({"type": tarfile.CHRTYPE}, b"")}, "special file")
        self.reject({"fifo.mio": ({"type": tarfile.FIFOTYPE}, b"")}, "special file")

    def test_rejects_executable_payloads_and_binary_content(self):
        self.reject({"a.mio": ({"mode": 0o755}, b"")}, "executable")
        self.reject({"a.mio": ({"mode": 0o4644}, b"")}, "executable")
        self.reject({"a.mio": b"\x7fELF\x02\x01"}, "executable or script payload")
        self.reject({"a.mio": b"MZ\x90\x00"}, "executable or script payload")
        self.reject({"a.mio": b"#!/bin/sh\n"}, "executable or script payload")
        self.reject({"a.mio": b"abc\0def"}, "NUL")
        self.reject({"a.mio": b"\xff\xfe\xfa"}, "UTF-8")

    def test_rejects_install_hooks_scripts_and_unexpected_files(self):
        for name in ("install.sh", "build.py", "setup.ps1", "run.bat", "lib.so", "tool.exe", "data.bin",
                     "image.png", "Makefile", ".github/workflows/ci.yml", "package.json", "a.MIO.bak"):
            with self.subTest(name=name):
                self.reject({name: b"echo hi\n"}, "not allowed in a source-only package")

    def test_rejects_nonportable_and_colliding_names(self):
        for name in ("a b.mio", "a:b.mio", "x\\y.mio", "con.mio", "NUL.txt", "trail.", "é.mio", "a\nb.mio", "a?.mio"):
            with self.subTest(name=name):
                self.reject({name: b""}, "")
        self.reject({"A.mio": b"", "a.mio": b""}, "case-colliding")
        self.reject({"a.mio": b"", "a.mio/b.mio": b""}, "both a file and a directory")

    def test_rejects_duplicate_entries_limits_and_corruption(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for _ in range(2):
                info = tarfile.TarInfo("a.mio")
                tar.addfile(info, io.BytesIO(b""))
        self.reject(buffer.getvalue(), "duplicate")
        self.reject(b"not a tarball", "not a readable")
        self.reject({}, "no files")
        whole = tarball({"a.mio": os.urandom(40000)})
        self.reject(whole[:len(whole) // 2], "corrupt archive")
        deep = "/".join(["d"] * (archive.MAX_DEPTH + 1)) + "/a.mio"
        self.reject({deep: b""}, "nested deeper")
        original = archive.MAX_FILE_BYTES, archive.MAX_FILES, archive.MAX_TOTAL_BYTES
        self.addCleanup(lambda: setattr_all(archive, original))
        archive.MAX_FILE_BYTES = 4
        self.reject({"a.mio": b"12345"}, "larger than")
        archive.MAX_FILE_BYTES, archive.MAX_FILES = original[0], 1
        self.reject({"a.mio": b"", "b.mio": b""}, "more than 1 files")
        archive.MAX_FILES, archive.MAX_TOTAL_BYTES = original[1], 5
        self.reject({"a.mio": b"123", "b.mio": b"456"}, "extracted size")

    def test_refuses_an_existing_destination(self):
        (self.dir / "out").mkdir()
        with self.assertRaises(archive.ArchiveError):
            self.extract({"a.mio": b""})

    def test_test_discovery_follows_mio_rules(self):
        root = self.dir / "pkg"
        for name in ("a_test.mio", "sub/b_test.mio", "sub/b.mio", "test.mio", "a_test.mio.txt",
                     "vendor/c_test.mio", "build/d_test.mio", ".hidden/e_test.mio", "node_modules/f_test.mio"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        self.assertEqual(archive.discover_tests(root), ["a_test.mio", "sub/b_test.mio"])
        self.assertEqual(archive.discover_tests(self.dir / "pkg/sub/b.mio"), [])


def setattr_all(module, original):
    module.MAX_FILE_BYTES, module.MAX_FILES, module.MAX_TOTAL_BYTES = original


class FakeFetcher:
    """Serves canned GitHub API responses and bytes; records every request."""

    def __init__(self):
        self.documents, self.files, self.requests = {}, {}, []

    def json(self, url):
        self.requests.append(url)
        if url not in self.documents:
            raise source.SourceError(f"HTTP 404 fetching {url}")
        return self.documents[url]

    def download(self, url, destination, max_bytes):
        self.requests.append(url)
        if url not in self.files:
            raise source.SourceError(f"HTTP 404 fetching {url}")
        data = self.files[url]
        if len(data) > max_bytes:
            raise source.SourceError("too large")
        Path(destination).write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)


class Index:
    """A throwaway Git index with fake releases for each record it creates."""

    def __init__(self, test, fetcher):
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name)
        self.root = self.scratch / "index"
        self.root.mkdir()
        self.fetcher = fetcher
        self.git("init", "-q")
        for key, value in (("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        self.commit("empty")
        self.counter = 100

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=T", "-c", "user.email=t@example.org", "commit", "-qm", message, "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def add(self, name, version="1.0.0", *, files=None, platforms=None, deps=None, mio=("2.0.0", "3.0.0"),
            immutable=True, draft=False, tamper_sha=False, hide_digest=False, tag_commit=None, annotated=False, archive_bytes=None):
        namespace, package = name.split("/")
        self.counter += 1
        release_id, asset_id = self.counter, self.counter + 5000
        if files is None:
            files = {f"{package}.mio": b"", f"{package}_test.mio": PASSING_TEST.encode()}
        data = archive_bytes if archive_bytes is not None else tarball(files)
        sha = hashlib.sha256(data).hexdigest()
        commit = "ab" * 20
        api = f"https://api.github.com/repos/{namespace}/{package}"
        archive_url = f"{api}/releases/assets/{asset_id}"
        platforms = ALL if platforms is None else platforms
        lines = [f'name = "{name}"', f'version = "{version}"', f'publisher = "github:{namespace}"',
                 'description = "A test package."', f'repository = "https://github.com/{namespace}/{package}"',
                 f'source_commit = "{commit}"', f'release_id = "{release_id}"', f'archive = "{archive_url}"',
                 f'sha256 = "{"0" * 64 if tamper_sha else sha}"', 'license = "MIT"', "",
                 "[mio]", f'min = "{mio[0]}"', f'before = "{mio[1]}"', "", "[platforms]"]
        lines += [f"{os_name} = {json.dumps(archs)}" for os_name, archs in platforms.items()]
        lines += ["", "[dependencies]"] + [f'"{d}" = "{v}"' for d, v in (deps or {}).items()]
        record = self.root / f"packages/{name}/{version}.toml"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tag = f"v{version}"
        self.fetcher.documents[f"{api}/releases/{release_id}"] = {
            "id": release_id, "draft": draft, "immutable": immutable, "tag_name": tag,
            "assets": [{"id": asset_id, "url": archive_url, "name": f"{package}.tar.gz", "size": len(data),
                        "state": "uploaded", "digest": None if hide_digest else f"sha256:{sha}"}]}
        target = tag_commit or commit
        if annotated:
            self.fetcher.documents[f"{api}/git/ref/tags/{tag}"] = {"object": {"type": "tag", "sha": "cd" * 20}}
            self.fetcher.documents[f"{api}/git/tags/{'cd' * 20}"] = {"object": {"type": "commit", "sha": target}}
        else:
            self.fetcher.documents[f"{api}/git/ref/tags/{tag}"] = {"object": {"type": "commit", "sha": target}}
        self.fetcher.files[archive_url] = data
        return f"packages/{name}/{version}.toml"


def fake_toolchain(fetcher, top="mio-test"):
    data = tarball({f"{top}/bin/mio": ({"mode": 0o755}, b"\x7fELF fake"),
                    f"{top}/bunko/kiso/testing/testing.mio": b"// stdlib\n"})
    rule = {"runner": "ubuntu-24.04", "mio_version": "2.1.0", "mio_asset": "mio-test.tar.gz",
            "mio_sha256": hashlib.sha256(data).hexdigest(), "mio_binary": "bin/mio"}
    fetcher.files["https://github.com/mio-kobo/mio/releases/download/v2.1.0/mio-test.tar.gz"] = data
    return {target: rule for target in ("linux/x86_64", "macos/arm64", "windows/x86_64")}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = FakeFetcher()
        self.index = Index(self, self.fetcher)
        self.base = self.index.git("rev-parse", "HEAD")

    def plan(self):
        head = self.index.commit("change")
        return ci.plan(self.index.root, self.base, head)["include"]

    def test_pull_request_without_package_records_has_an_empty_matrix(self):
        (self.index.root / "README.md").write_text("docs\n")
        self.assertEqual(self.plan(), [])

    def test_one_platform_package_creates_one_job(self):
        self.index.add("alice/json", platforms={"linux": ["x86_64"]})
        jobs = self.plan()
        self.assertEqual([(j["target"], j["runner"]) for j in jobs], [("linux/x86_64", "ubuntu-24.04")])
        self.assertEqual(jobs[0]["record"], "packages/alice/json/1.0.0.toml")

    def test_multi_platform_package_uses_the_pinned_runners(self):
        self.index.add("alice/json")
        jobs = self.plan()
        self.assertEqual({j["target"]: j["runner"] for j in jobs}, {
            "linux/x86_64": "ubuntu-24.04", "macos/arm64": "macos-15", "windows/x86_64": "windows-2025"})

    def test_undeclared_windows_target_is_not_in_the_matrix(self):
        self.index.add("alice/json", platforms={"linux": ["x86_64"], "macos": ["arm64"]})
        targets = {j["target"] for j in self.plan()}
        self.assertEqual(targets, {"linux/x86_64", "macos/arm64"})
        self.assertNotIn("windows/x86_64", targets)

    def test_only_added_records_are_tested(self):
        self.index.add("alice/json")
        self.base = self.index.commit("published")
        (self.index.root / "packages/alice/json/1.0.0.toml").write_text("# changed\n")
        self.index.add("alice/json", "1.1.0", platforms={"windows": ["x86_64"]})
        jobs = self.plan()
        self.assertEqual([(j["version"], j["target"]) for j in jobs], [("1.1.0", "windows/x86_64")])

    def test_matrix_is_deterministic_and_contains_only_validated_fields(self):
        self.index.add("bob/zeta")
        self.index.add("alice/json")
        first = self.plan()
        self.assertEqual(first, self.plan())
        self.assertEqual([j["name"] for j in first], ["alice/json"] * 3 + ["bob/zeta"] * 3)
        for job in first:
            validate.path_release(job["record"])
            self.assertRegex(job["label"], r"^[a-z0-9/@.\- _]+$")

    def test_unsupported_targets_and_excluded_compilers_are_errors(self):
        self.index.add("alice/json", platforms={"linux": ["arm64"]})
        with self.assertRaises(ci.CiError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "BUNKO_E_PLATFORM")

    def test_mio_range_that_excludes_the_test_compiler_fails(self):
        self.index.add("alice/json", mio=("2.0.0", "2.1.0"))
        with self.assertRaises(ci.CiError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "BUNKO_E_TEST_COMPILER")
        self.assertIn("excludes Mio 2.1.0", str(caught.exception))

    def test_invalid_platform_metadata_fails_planning(self):
        path = self.index.add("alice/json")
        record = self.index.root / path
        record.write_text(record.read_text().replace('linux = ["x86_64"]', 'linux = ["amd64"]'))
        with self.assertRaises(ci.CiError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "BUNKO_E_SCHEMA")

    def test_too_many_new_records_in_one_pull_request(self):
        for number in range(ci.MAX_NEW_RECORDS + 1):
            self.index.add(f"alice/p{number}", platforms={"linux": ["x86_64"]})
        with self.assertRaises(ci.CiError) as caught:
            self.plan()
        self.assertIn("split the PR", str(caught.exception))

    def test_command_writes_github_outputs(self):
        self.index.add("alice/json", platforms={"linux": ["x86_64"]})
        head = self.index.commit("change")
        out = self.index.scratch / "output.txt"
        out.write_text("")
        env = dict(os.environ, GITHUB_OUTPUT=str(out))
        subprocess.run([sys.executable, "-I", str(INDEX / "tools/bunko_ci.py"), "plan", "--root",
                        str(self.index.root), "--base", self.base, "--head", head],
                       check=True, capture_output=True, env=env)
        lines = out.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0].removeprefix("matrix="))["include"][0]["target"], "linux/x86_64")
        self.assertEqual(lines[1], "has_packages=true")


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = FakeFetcher()
        self.index = Index(self, self.fetcher)
        self.dest = self.index.scratch / "download.tar.gz"

    def record(self, **options):
        path = self.index.add("alice/json", **options)
        return ci.read_record(self.index.root, self.index.commit("add"), path)

    def fetch(self, **options):
        source.fetch_package_archive(self.record(**options), self.fetcher, self.dest)

    def rejects(self, text, **options):
        with self.assertRaises(source.SourceError) as caught:
            self.fetch(**options)
        self.assertIn(text, str(caught.exception))

    def test_verified_release_downloads(self):
        self.fetch()
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.read_bytes(), next(iter(self.fetcher.files.values())))

    def test_annotated_tags_are_peeled_to_the_source_commit(self):
        self.fetch(annotated=True)

    def test_rejects_draft_mutable_and_wrong_commit_releases(self):
        self.rejects("draft", draft=True)
        self.rejects("not immutable", immutable=False)
        self.rejects("does not resolve to source_commit", tag_commit="ef" * 20)
        self.rejects("does not resolve to source_commit", tag_commit="ef" * 20, annotated=True)

    def test_rejects_a_modified_archive_before_extraction(self):
        self.rejects("digest", tamper_sha=True)  # GitHub's own digest already disagrees
        self.assertFalse(self.dest.exists())
        self.rejects("SHA-256", tamper_sha=True, hide_digest=True)  # the downloaded bytes are hashed too
        self.assertFalse(self.dest.exists(), "a mismatching archive must be deleted")

    def test_rejects_a_missing_release_or_asset(self):
        record = self.record()
        del self.fetcher.documents[f"https://api.github.com/repos/alice/json/releases/{record['release_id']}"]
        with self.assertRaises(source.SourceError):
            source.verify_release(record, self.fetcher)

    def test_rejects_asset_not_in_release_or_not_a_tarball(self):
        record = self.record()
        url = f"https://api.github.com/repos/alice/json/releases/{record['release_id']}"
        release = self.fetcher.documents[url]
        release["assets"][0]["name"] = "json.zip"
        with self.assertRaisesRegex(source.SourceError, "tar.gz"):
            source.verify_release(record, self.fetcher)
        release["assets"][0]["name"] = "json.tar.gz"
        release["assets"][0]["id"] += 1
        with self.assertRaisesRegex(source.SourceError, "not part of release"):
            source.verify_release(record, self.fetcher)

    def test_rejects_conflicting_github_digest(self):
        record = self.record()
        url = f"https://api.github.com/repos/alice/json/releases/{record['release_id']}"
        self.fetcher.documents[url]["assets"][0]["digest"] = "sha256:" + "1" * 64
        with self.assertRaisesRegex(source.SourceError, "digest"):
            source.verify_release(record, self.fetcher)

    def test_toolchain_checksum_is_pinned(self):
        rules = fake_toolchain(self.fetcher)
        source.fetch_toolchain(rules["linux/x86_64"], self.fetcher, self.dest)
        rule = dict(rules["linux/x86_64"], mio_sha256="0" * 64)
        with self.assertRaisesRegex(source.SourceError, "pinned value"):
            source.fetch_toolchain(rule, self.fetcher, self.dest)

    def test_downloads_only_follow_github_hosts_over_https(self):
        allowed = source._allowed_download_host
        self.assertTrue(allowed("https://release-assets.githubusercontent.com/x"))
        self.assertTrue(allowed("https://github.com/mio-kobo/mio/releases/download/v2.1.0/a"))
        for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x",
                    "https://user@github.com/x", "https://githubusercontent.com.evil.example/x",
                    "https://github.com:8443/x"):
            with self.subTest(url=url):
                self.assertFalse(allowed(url))

    def test_json_is_only_fetched_from_the_api_host(self):
        with self.assertRaises(source.SourceError):
            source.Fetcher().json("https://example.org/x")

    def test_credentials_are_only_sent_to_the_api_host(self):
        seen = []

        class Recorder(source.Fetcher):
            def _open(self, url, accept, authenticate):
                request_headers = {}
                if authenticate and self.token and source.urllib.parse.urlsplit(url).hostname == source.API_HOST:
                    request_headers["Authorization"] = "sent"
                seen.append((url, request_headers))
                raise source.urllib.error.HTTPError(url, 404, "nf", {}, None)

        fetcher = Recorder(token="secret", sleep=lambda _: None)
        for url in ("https://api.github.com/x", "https://github.com/y"):
            with self.assertRaises(source.SourceError):
                fetcher._request(url, "*/*")
        self.assertEqual(seen[0][1], {"Authorization": "sent"})
        self.assertEqual(seen[1][1], {})


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = FakeFetcher()
        self.rules = fake_toolchain(self.fetcher)
        self.index = Index(self, self.fetcher)
        self.work = self.index.scratch / "work"

    def prepare(self, path, target="linux/x86_64"):
        ref = self.index.commit("add")
        return ci.prepare(self.index.root, ref, path, target, self.work, self.fetcher, self.rules)

    def fails(self, path, code, text="", target="linux/x86_64"):
        with self.assertRaises(ci.CiError) as caught:
            self.prepare(path, target)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertIn(text, str(caught.exception))

    def test_prepares_package_dependencies_and_compiler_in_the_bunko_root(self):
        self.index.add("alice/json", files={"json.mio": b"", "json_test.mio": PASSING_TEST.encode()})
        path = self.index.add("bob/app", deps={"alice/json": "1.0.0"})
        manifest = self.prepare(path)
        root = Path(manifest["bunko_root"])
        self.assertTrue((root / "kiso/testing/testing.mio").is_file())
        self.assertTrue((root / "alice/json/json.mio").is_file())
        self.assertTrue((root / "bob/app/app_test.mio").is_file())
        self.assertEqual(manifest["closure"], ["alice/json@1.0.0", "bob/app@1.0.0"])
        self.assertEqual(manifest["tests"], ["app_test.mio"])
        self.assertTrue(os.access(manifest["mio"], os.X_OK))

    def test_missing_test_files_fail_a_new_package_version(self):
        path = self.index.add("alice/json", files={"json.mio": b"", "README.md": b"docs\n"})
        self.fails(path, "BUNKO_E_NO_TESTS", "kiso/testing")

    def test_tests_in_excluded_directories_do_not_count(self):
        path = self.index.add("alice/json", files={"json.mio": b"", "vendor/x_test.mio": PASSING_TEST.encode()})
        self.fails(path, "BUNKO_E_NO_TESTS")

    def test_unsafe_package_archives_fail_before_any_test_runs(self):
        for label, entries in {
            "traversal": {"../x.mio": b"", "a_test.mio": PASSING_TEST.encode()},
            "symlink": {"a_test.mio": PASSING_TEST.encode(), "l.mio": ({"type": tarfile.SYMTYPE, "linkname": "/etc"}, b"")},
            "executable": {"a_test.mio": PASSING_TEST.encode(), "run.sh": ({"mode": 0o755}, b"echo\n")},
        }.items():
            with self.subTest(case=label):
                self.work = self.index.scratch / f"work-{label}"
                path = self.index.add(f"alice/{label}", files=entries)
                self.fails(path, "BUNKO_E_ARCHIVE")

    def test_unsafe_dependency_archive_fails_the_dependent(self):
        self.index.add("alice/json", files={"json.mio": b"", "x.so": b"binary"})
        path = self.index.add("bob/app", deps={"alice/json": "1.0.0"})
        self.fails(path, "BUNKO_E_ARCHIVE", "alice/json@1.0.0")

    def test_checksum_and_provenance_failures_are_reported(self):
        path = self.index.add("alice/json", tamper_sha=True, hide_digest=True)
        with self.assertRaisesRegex(source.SourceError, "SHA-256"):
            self.prepare(path)

    def test_dependency_closure_platform_gap_stops_the_job(self):
        self.index.add("alice/json", platforms={"linux": ["x86_64"]})
        path = self.index.add("bob/app", deps={"alice/json": "1.0.0"})  # declares all three targets
        for target in ("linux/x86_64", "windows/x86_64"):
            with self.subTest(target=target):
                self.work = self.index.scratch / f"work-{target[:3]}"
                self.fails(path, "BUNKO_E_PLATFORM_DEPENDENCY",
                           "bob/app@1.0.0 -> alice/json@1.0.0 does not declare macos/arm64, windows/x86_64",
                           target=target)

    def test_a_package_may_declare_only_targets_its_closure_supports(self):
        self.index.add("alice/json", platforms={"linux": ["x86_64"]})
        path = self.index.add("bob/app", platforms={"linux": ["x86_64"]}, deps={"alice/json": "1.0.0"})
        self.assertEqual(self.prepare(path)["closure"], ["alice/json@1.0.0", "bob/app@1.0.0"])

    def test_undeclared_target_cannot_be_prepared(self):
        path = self.index.add("alice/json", platforms={"linux": ["x86_64"]})
        self.fails(path, "BUNKO_E_PLATFORM", "does not declare windows/x86_64", target="windows/x86_64")
        self.fails(path, "BUNKO_E_PLATFORM", target="linux/arm64")

    def test_revoked_and_missing_dependencies_stop_the_job(self):
        self.index.add("alice/json")
        path = self.index.add("bob/app", deps={"alice/json": "1.0.0"})
        missing = self.index.add("carol/app", deps={"dan/missing": "1.0.0"})
        # Revocations come from the trusted tooling checkout, never from the PR tree.
        trusted = self.index.scratch / "trusted"
        for directory in ("schemas", "policies"):
            shutil.copytree(INDEX / directory, trusted / directory)
        (trusted / "policies/revoked-versions.toml").write_text(
            '[revoked]\n"alice/json@1.0.0" = "https://example.org/advisory/1"\n')
        with mock.patch.object(validate, "INDEX_HOME", trusted):
            self.fails(path, "BUNKO_E_REVOKED", "alice/json@1.0.0 is revoked")
        self.fails(missing, "BUNKO_E_MISSING_PACKAGE")

    def test_standard_library_namespaces_cannot_be_shadowed(self):
        path = self.index.add("kiso/testing", files={"t_test.mio": PASSING_TEST.encode()})
        # Reserved namespaces are refused by the validator before anything is unpacked.
        self.fails(path, "BUNKO_E_INVALID_DEPENDENCY")

    def test_every_job_uses_the_pinned_compiler_checksum(self):
        path = self.index.add("alice/json")
        self.rules["linux/x86_64"]["mio_sha256"] = "0" * 64
        with self.assertRaises(source.SourceError):
            self.prepare(path)


class ReportTests(unittest.TestCase):
    def report(self, packages, summary, schema_version=1):
        return json.dumps({"schema_version": schema_version, "packages": packages, "summary": summary})

    def passed(self, path="alice/json"):
        return {"path": path, "status": "passed", "tests": [{"name": "test_ok", "path": "/x/a_test.mio", "status": "passed"}]}

    def test_passing_report(self):
        ok, passed, failed, lines = ci.judge_report(
            self.report([self.passed()], {"status": "passed", "passed": 1, "failed": 0}), 0, {"alice/json"})
        self.assertEqual((ok, passed, failed, lines), (True, 1, 0, []))

    def test_failed_test_propagates_with_name_and_message(self):
        package = {"path": "alice/json", "status": "failed", "tests": [
            {"name": "test_bad", "path": "/w/bad_test.mio", "status": "failed",
             "failures": [{"code": "E0602", "message": "expected 1\u001b[31m"}]}]}
        ok, _, failed, lines = ci.judge_report(
            self.report([package], {"status": "failed", "passed": 0, "failed": 1}), 1, {"alice/json"})
        self.assertFalse(ok)
        self.assertEqual(failed, 1)
        self.assertIn("FAIL test_bad (bad_test.mio): expected 1\\x1b[31m", lines)

    def test_failures_cannot_hide_behind_a_zero_exit_status_or_status_text(self):
        package = {"path": "alice/json", "status": "failed", "tests": [{"name": "t", "status": "failed"}]}
        for summary in ({"status": "passed", "passed": 1, "failed": 1}, {"status": "failed", "passed": 1, "failed": 1}):
            ok, *_ = ci.judge_report(self.report([package], summary), 0, {"alice/json"})
            self.assertFalse(ok)

    def test_nonzero_exit_fails_even_with_a_clean_report(self):
        ok, *_ , lines = ci.judge_report(
            self.report([self.passed()], {"status": "passed", "passed": 1, "failed": 0}), 3, {"alice/json"})
        self.assertFalse(ok)
        self.assertIn("exited with status 3", lines[-1])

    def test_no_tests_is_a_failure_even_though_mio_exits_zero(self):
        ok, _, _, lines = ci.judge_report(
            self.report([], {"status": "no_tests", "passed": 0, "failed": 0}), 0, {"alice/json"})
        self.assertFalse(ok)
        self.assertTrue(any("no tests" in line for line in lines))

    def test_compile_errors_are_reported(self):
        package = {"path": "alice/json", "status": "compile_failed", "tests": [],
                   "error": {"code": "E0605", "message": "E_PARSE a.mio:1:2"}}
        ok, _, _, lines = ci.judge_report(
            self.report([package], {"status": "failed", "passed": 0, "failed": 1}), 1, {"alice/json"})
        self.assertFalse(ok)
        self.assertIn("compile error in alice/json: E_PARSE a.mio:1:2", lines)

    def test_every_discovered_test_directory_must_be_reported(self):
        ok, _, _, lines = ci.judge_report(
            self.report([self.passed()], {"status": "passed", "passed": 1, "failed": 0}), 0,
            {"alice/json", "alice/json/sub"})
        self.assertFalse(ok)
        self.assertIn("test directory alice/json/sub was not run by mio test", lines)

    def test_windows_style_report_paths_match(self):
        ok, *_ = ci.judge_report(
            self.report([self.passed("alice\\json\\sub")], {"status": "passed", "passed": 1, "failed": 0}), 0,
            {"alice/json/sub"})
        self.assertTrue(ok)

    def test_garbage_and_unknown_schema_versions_fail(self):
        for text in ("", "not json", "[]", self.report([], {}, 2),
                     self.report([], {"status": "passed", "passed": 1, "failed": 0}, 2)):
            with self.subTest(text=text[:20]):
                self.assertFalse(ci.judge_report(text, 0, set())[0])


class RunTests(unittest.TestCase):
    """`run` with a stand-in compiler, to check environment and command construction."""

    @unittest.skipIf(os.name == "nt", "uses a POSIX shell script as the stand-in compiler")
    def test_untrusted_tests_run_without_credentials_in_a_clean_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            root = work / "bunko"
            root.mkdir()
            fake = work / "mio"
            fake.write_text(
                "#!/bin/sh\nenv > \"$MIO_BUNKO/../env.txt\"\nprintf '%s\\n' \"$*\" > \"$MIO_BUNKO/../args.txt\"\n"
                "echo '{\"schema_version\":1,\"packages\":[{\"path\":\"alice/json\",\"status\":\"passed\",\"tests\":[]}],"
                "\"summary\":{\"status\":\"passed\",\"passed\":1,\"failed\":0}}'\n")
            fake.chmod(0o755)
            (work / "manifest.json").write_text(json.dumps({
                "target": "linux/x86_64", "runner": "ubuntu-24.04", "name": "alice/json", "version": "1.0.0",
                "mio": str(fake), "bunko_root": str(root), "tests": ["json_test.mio"], "closure": [],
                "mio_version": "2.1.0"}))
            secrets = {"GH_TOKEN": "t0ken", "GITHUB_TOKEN": "t0ken", "ACTIONS_RUNTIME_TOKEN": "x", "AWS_SECRET": "y"}
            previous = {k: os.environ.get(k) for k in secrets}
            os.environ.update(secrets)
            try:
                manifest, ok, passed, failed, lines, stderr = ci.run(work)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertTrue(ok, lines)
            env = (work / "env.txt").read_text()
            for name, value in secrets.items():
                self.assertNotIn(name, env)
            self.assertIn(f"MIO_BUNKO={root}", env)
            self.assertEqual((work / "args.txt").read_text().strip(),
                             "test alice/json/... --format=json --color=never --lang=en-US")


if __name__ == "__main__":
    unittest.main()
