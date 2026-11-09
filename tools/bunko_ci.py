#!/usr/bin/env python3
"""Trusted Bunko package-test orchestration for GitHub-hosted CI.

Run this file from the base branch, never from the pull request. Package
publishers control only the immutable release archive: the targets, runners,
compiler, commands and environment below are owned by this repository.

  plan     list newly added package records and build the job matrix
  prepare  verify and extract one record, its dependency closure and the compiler
  run      run `mio test` for the prepared package and judge the JSON report
"""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bunko_archive  # noqa: E402
import bunko_source  # noqa: E402
import validate  # noqa: E402

MAX_NEW_RECORDS = 20
TEST_TIMEOUT_SECONDS = 15 * 60
MAX_REPORT_BYTES = 8 * 1024 * 1024
ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "LANG")
TARGET = re.compile(r"(linux|macos|windows)/(x86_64|arm64)")


class CiError(ValueError):
    """A trusted-tooling check failed; `code` follows the BUNKO_E_* convention."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def read_record(root, ref, path):
    document = validate.read_toml(validate.git(root, "cat-file", "blob", f"{ref}:{path}"))
    errors = list(validate.schema_errors(document, json.loads(
        (validate.INDEX_HOME / "schemas/package.schema.json").read_text(encoding="utf-8"))))
    if errors:
        raise CiError("BUNKO_E_SCHEMA", f"{path}: " + "; ".join(f"{f} {m}" for f, m in errors))
    return document


def added_records(root, base, head):
    """Package record paths added between base and head (Git tree comparison)."""
    output = validate.git(root, "diff", "--name-status", "-z", "--no-renames", "--diff-filter=A",
                          base, head, "--", "packages")
    fields = output.split(b"\0")
    paths = [os.fsdecode(fields[i + 1]) for i in range(0, len(fields) - 1, 2) if fields[i] == b"A"]
    for path in paths:
        try:
            validate.path_release(path)
        except ValueError as error:
            raise CiError("BUNKO_E_RECORD", f"{path}: {error}") from None
    return sorted(paths)


def version_tuple(value):
    return tuple(int(part) for part in value.split("."))


def plan(root, base, head):
    """Return the matrix for new records: exactly their declared, supported targets."""
    targets = validate.load_targets()
    records = added_records(root, base, head)
    if len(records) > MAX_NEW_RECORDS:
        raise CiError("BUNKO_E_TEST_PLAN", f"{len(records)} new records exceed the limit of {MAX_NEW_RECORDS} per PR; split the PR")
    include = []
    for path in records:
        document = read_record(root, head, path)
        for target in validate.declared_targets(document):
            rule = targets.get(target)
            if rule is None:
                raise CiError("BUNKO_E_PLATFORM", f"{path}: {target} is not a supported V1 target")
            compiler = version_tuple(rule["mio_version"])
            if not version_tuple(document["mio"]["min"]) <= compiler < version_tuple(document["mio"]["before"]):
                raise CiError("BUNKO_E_TEST_COMPILER",
                              f"{path}: [mio] range {document['mio']['min']} <= v < {document['mio']['before']} "
                              f"excludes Mio {rule['mio_version']}, the compiler Bunko tests {target} with")
            os_name, arch = target.split("/")
            include.append({
                "name": document["name"], "version": document["version"], "record": path,
                "target": target, "os": os_name, "arch": arch, "runner": rule["runner"],
                "label": f"{document['name']}@{document['version']} {target}",
            })
    return {"include": include}


def closure_of(packages, root_key):
    """Releases reachable from root_key in dependency order (dependencies first)."""
    order, seen = [], set()

    def visit(key):
        if key in seen:
            return
        seen.add(key)
        for dependency in packages[key].dependencies:
            visit(dependency)
        order.append(key)

    visit(root_key)
    return order


def prepare(root, ref, record_path, target, work, fetcher, rules=None):
    """Verify and unpack everything one matrix job needs, without running package code."""
    if not TARGET.fullmatch(target):
        raise CiError("BUNKO_E_PLATFORM", f"invalid target {target!r}")
    rules = rules or validate.load_targets()
    if target not in rules:
        raise CiError("BUNKO_E_PLATFORM", f"{target} is not a supported V1 target")
    rule = rules[target]
    root_key = validate.path_release(record_path)

    settings, revoked = validate.settings_for(root, None, None)
    entries = validate.git_entries(root, ref)
    packages, invalid, issues = validate.validate_records(root, entries, {}, settings)
    problems = [i for i in issues if not i.warning]
    if root_key not in packages:
        raise CiError("BUNKO_E_INVALID_DEPENDENCY", f"{record_path} is not a valid record: "
                      + "; ".join(i.message for i in problems if i.path == record_path))
    names = {name for name, _ in packages.keys() | invalid.keys()}
    graph = validate.check_graph([root_key], packages, invalid, revoked, names)
    if graph:
        raise CiError(graph[0].code, "; ".join(i.message for i in graph))
    if target not in packages[root_key].platforms:
        raise CiError("BUNKO_E_PLATFORM", f"{validate.release_label(root_key)} does not declare {target}")

    work = Path(work)
    work.mkdir(parents=True)
    downloads = work / "downloads"
    downloads.mkdir()
    compiler_archive = downloads / rule["mio_asset"]
    bunko_source.fetch_toolchain(rule, fetcher, compiler_archive)
    try:
        binary = bunko_archive.extract_toolchain(compiler_archive, work / "mio", rule["mio_binary"])
    except (OSError, ValueError, bunko_archive.zipfile.BadZipFile, bunko_archive.tarfile.TarError) as error:
        raise CiError("BUNKO_E_TOOLCHAIN", f"cannot unpack {rule['mio_asset']}: {error}") from None
    bunko_root = work / "mio" / "bunko"
    if not (bunko_root / "kiso").is_dir():
        raise CiError("BUNKO_E_TOOLCHAIN", f"{rule['mio_asset']} has no bunko/kiso standard library")

    closure = closure_of(packages, root_key)
    for key in closure:
        document = read_record(root, ref, packages[key].path)
        name = f"{key[0].replace('/', '__')}__{key[1]}.tar.gz"
        archive = downloads / name
        bunko_source.fetch_package_archive(document, fetcher, archive)
        destination = bunko_root.joinpath(*key[0].split("/"))
        if destination.exists():
            raise CiError("BUNKO_E_ARCHIVE", f"{key[0]} collides with a path already in the Mio standard library")
        try:
            files = bunko_archive.extract_package(archive, destination)
        except bunko_archive.ArchiveError as error:
            raise CiError("BUNKO_E_ARCHIVE", f"{validate.release_label(key)}: {error}") from None
        if key == root_key:
            tests = bunko_archive.discover_tests(destination)
            if not tests:
                raise CiError("BUNKO_E_NO_TESTS",
                              f"{validate.release_label(key)} has no *_test.mio file; add one that uses "
                              "kiso/testing (see docs/testing.md)")
            print(f"{validate.release_label(key)}: {len(files)} file(s), {len(tests)} test file(s)")
        else:
            print(f"dependency {validate.release_label(key)}: {len(files)} file(s) verified")

    manifest = {
        "target": target, "runner": rule["runner"], "name": root_key[0], "version": root_key[1],
        "mio": str(binary), "bunko_root": str(bunko_root), "tests": tests,
        "closure": [validate.release_label(key) for key in closure], "mio_version": rule["mio_version"],
    }
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def clean_environment(work):
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    home = Path(work) / "home"
    scratch = Path(work) / "tmp"
    home.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch))
    return env


def judge_report(stdout, returncode, expected_dirs):
    """Return (ok, passed, failed, lines). The report, not the exit status alone, decides."""
    try:
        report = json.loads(stdout[:MAX_REPORT_BYTES])
        summary = report["summary"]
        packages = report["packages"]
        assert report["schema_version"] == 1
        passed, failed = int(summary["passed"]), int(summary["failed"])
    except (ValueError, KeyError, TypeError, AssertionError):
        return False, 0, 0, ["mio test did not produce a valid schema_version 1 JSON report"]
    lines = []
    for package in packages:
        if package.get("status") == "compile_failed" or package.get("error"):
            lines.append(f"compile error in {bunko_archive.printable(package.get('path'))}: "
                         f"{bunko_archive.printable((package.get('error') or {}).get('message'), 400)}")
        for test in package.get("tests", []):
            if test.get("status") != "passed":
                where = bunko_archive.printable(PurePosixPath(str(test.get("path", "")).replace("\\", "/")).name)
                detail = "; ".join(bunko_archive.printable(f.get("message"), 200) for f in test.get("failures", []))
                if test.get("error"):
                    detail += bunko_archive.printable((test["error"] or {}).get("message"), 300)
                lines.append(f"FAIL {bunko_archive.printable(test.get('name'))} ({where}): {detail}")
    # Mio prints package paths as given on the command line, relative to its working directory.
    reported = {str(p.get("path", "")).replace("\\", "/").removeprefix("./") for p in packages}
    missing = sorted(expected_dirs - reported)
    ok = summary.get("status") == "passed" and returncode == 0 and failed == 0 and passed > 0 and not missing
    if summary.get("status") == "no_tests":
        lines.append("mio test found no tests to run; Bunko requires at least one passing test")
    for directory in missing:
        lines.append(f"test directory {bunko_archive.printable(directory)} was not run by mio test")
    if returncode != 0 and not lines:
        lines.append(f"mio test exited with status {returncode}")
    return ok, passed, failed, lines


def run(work):
    manifest = json.loads((Path(work) / "manifest.json").read_text(encoding="utf-8"))
    env = clean_environment(work)
    env["MIO_BUNKO"] = manifest["bunko_root"]
    prefix = f"{manifest['name']}/..."
    # Package paths are relative to the Bunko root, matching how imports resolve.
    command = [manifest["mio"], "test", prefix, "--format=json", "--color=never", "--lang=en-US"]
    try:
        result = subprocess.run(command, cwd=manifest["bunko_root"], env=env, capture_output=True,
                                timeout=TEST_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise CiError("BUNKO_E_TEST_FAILED", f"mio test exceeded {TEST_TIMEOUT_SECONDS} seconds") from None
    stdout = result.stdout.decode("utf-8", "replace")
    expected_dirs = {str(PurePosixPath(manifest["name"], t).parent) for t in manifest["tests"]}
    ok, passed, failed, lines = judge_report(stdout, result.returncode, expected_dirs)
    stderr = result.stderr.decode("utf-8", "replace").strip()
    return manifest, ok, passed, failed, lines, stderr


def summary_row(manifest, ok, passed, failed):
    status = "passed" if ok else "FAILED"
    return (f"| `{manifest['name']}@{manifest['version']}` | `{manifest['target']}` | `{manifest['runner']}` "
            f"| Mio {manifest['mio_version']} | {status} | {passed} | {failed} |\n")


def append_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text)


def write_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        assert "\n" not in value
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--root", type=Path, default=Path.cwd())
    plan_parser.add_argument("--base", required=True)
    plan_parser.add_argument("--head", default="HEAD")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, default=Path.cwd())
    prepare_parser.add_argument("--ref", default="HEAD")
    prepare_parser.add_argument("--record", required=True)
    prepare_parser.add_argument("--target", required=True)
    prepare_parser.add_argument("--work", type=Path, required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            base = validate.git_ref(args.root, args.base)
            head = validate.git_ref(args.root, args.head)
            matrix = plan(args.root, base, head)
            encoded = json.dumps(matrix, separators=(",", ":"), sort_keys=True)
            write_output("matrix", encoded)
            write_output("has_packages", "true" if matrix["include"] else "false")
            print(encoded)
            if not matrix["include"]:
                print("No new package records; nothing to test.")
                append_summary("### Package tests\n\nNo new package records in this pull request.\n")
            return 0
        if args.command == "prepare":
            fetcher = bunko_source.Fetcher(token=os.environ.get("GH_TOKEN"))
            prepare(args.root, validate.git_ref(args.root, args.ref), args.record, args.target, args.work, fetcher)
            return 0
        manifest, ok, passed, failed, lines, stderr = run(args.work)
    except (CiError, bunko_source.SourceError, bunko_archive.ArchiveError) as error:
        code = error.code
        validate.report(validate.Issue(code, "", str(error)), os.environ.get("GITHUB_ACTIONS") == "true")
        append_summary(f"### Package tests\n\n`{code}`: {bunko_archive.printable(error, 400)}\n")
        return 1
    append_summary(
        "| Package | Target | Runner | Compiler | Result | Passed | Failed |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        + summary_row(manifest, ok, passed, failed)
        + ("" if ok else "\n" + "\n".join(f"- `{line.replace('`', chr(39))}`" for line in lines) + "\n"))
    github = os.environ.get("GITHUB_ACTIONS") == "true"
    label = f"{manifest['name']}@{manifest['version']} on {manifest['target']}"
    if ok:
        print(f"PASS {label}: {passed} test(s) passed with Mio {manifest['mio_version']}")
        return 0
    for line in lines:
        validate.report(validate.Issue("BUNKO_E_TEST_FAILED", "", f"{label}: {line}"), github)
    if stderr:
        print(bunko_archive.printable(stderr, 4000), file=sys.stderr)
    print(f"FAIL {label}: {passed} passed, {failed} failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
