# Testing packages

Every new package version is tested automatically on GitHub-hosted runners
before maintainers review it. Publishers supply Mio source and tests inside the
immutable release archive. They do not supply or change the test workflow,
runner selection, commands, install hooks or shell scripts.

## Writing tests

Tests use the compiler's own test runner, `mio test`. A test file is named
`*_test.mio` and lives in the package directory or any subdirectory. A test is a
top-level function that starts with `test_`, takes no parameters, is not
generic and returns `unit`. Import `kiso/testing` for `testing.expect(condition,
message)` and `testing.fail(message)`:

```text
yobu "kiso/testing";

waza test_answer() -> unit {
    testing.expect(6 * 7 == 42, "answer");
}
```

`_test.mio` files are loaded only by `mio test`, not by `check`, `run`, `build`
or imports of the package. Directories that begin with `.` and the directories
`build`, `dist`, `node_modules`, `vendor`, `cache` and `tmp` are skipped, exactly
as `mio test` skips them, so tests inside them do not count. The authoritative
syntax is in the [Mio test documentation](https://github.com/mio-kobo/mio/blob/v2.1.0/docs/testing.md)
and the `mio test` implementation (`cmd/mio/test_command.go` at tag `v2.1.0`).

A release with no discoverable test file, or one whose tests all fail to run,
does not pass. `mio test` itself exits `0` with status `no_tests` when it finds
no test functions, so Bunko reads the JSON report and requires at least one
passed test and no failures.

## What CI does

`.github/workflows/test-packages.yml` runs on `pull_request`:

1. **Plan.** The base branch's `tools/bunko_ci.py` finds package records the PR
   adds. For each record it reads `[platforms]` and creates one job per declared
   target, using the runner in `policies/platform-targets.toml`:

   | Target | Runner | Compiler asset |
   | --- | --- | --- |
   | `linux/x86_64` | `ubuntu-24.04` | `mio-2.1.0-linux-amd64-full.tar.gz` |
   | `macos/arm64` | `macos-15` | `mio-2.1.0-darwin-arm64-full.tar.gz` |
   | `windows/x86_64` | `windows-2025` | `mio-2.1.0-windows-amd64-full.zip` |

   Undeclared targets get no job. A PR that adds no package record gets an empty
   matrix and a passing `Package tests` check. Modified or deleted records are
   rejected by the validator, not tested.
2. **Verify.** Each job downloads the pinned Mio v2.1.0 asset and checks its
   SHA-256 against the repository policy. It then checks, for the package and
   every release in its dependency closure, that the GitHub release is published
   and immutable, that the asset belongs to it, that the tag resolves to
   `source_commit`, and that the downloaded bytes match `sha256`.
3. **Extract.** Archives are extracted under the policy in
   [package format](package-format.md#archive-contents). Any violation fails the
   job before any package code runs.
4. **Test.** Dependencies are placed beside the Mio standard library at
   `<namespace>/<package>`, the Bunko root is set with `MIO_BUNKO`, and the job
   runs `mio test <namespace>/<package>/... --format=json --color=never --lang=en-US`.
   This step receives no credentials. The job fails if Mio exits nonzero, any test
   or compilation fails, no test passes, or a directory with test files was not run.

Each job writes a table to the GitHub step summary and emits annotations with the
failing test names and messages. Jobs use `fail-fast: false`, so one failing
target does not hide the others. Jobs time out after 25 minutes and a
superseded push cancels the previous run. The final `Package tests` job passes
only if planning and tooling passed and either every matrix job passed or the
plan explicitly reported no new package records. A skipped matrix for a PR that
does add records fails.

The `Test tooling` jobs run this repository's own tests on all three runners
against the real pinned compiler, so a broken harness fails CI instead of
silently passing packages.

## Platform support and manual hardware

Only the three targets above are testable for Mio v2.1.0. A target is added to
`policies/platform-targets.toml` only after verifying an official compiler
release and a compatible GitHub-hosted runner. CI never uses self-hosted
runners and never dispatches to private hardware.

Testing on dedicated hardware, for example another architecture or an older
machine, is a separate manual maintainer process after source review. A manual
result does not change `[platforms]`: a target the index cannot test remains
unsupported.

## Security model

Package archives, test source, file names and metadata are untrusted.

* The workflow uses `pull_request` with `contents: read`, no secrets, and
  `persist-credentials: false`. It never uses `pull_request_target`.
* Orchestration is run from the PR's base commit. A PR that edits the tooling is
  tested by the old tooling, and CODEOWNERS review applies to changes under
  `.github/`, `tools/` and `policies/`.
* PR-supplied strings reach shell steps only through environment variables, and
  matrix values are validated record paths and fixed policy values.
* The test process gets a minimal environment with no tokens and a temporary
  home. Mio tests can still use the network and the runner's disk, so the
  runners must stay GitHub-hosted and ephemeral.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `BUNKO_E_NO_TESTS ... has no *_test.mio file` | Add a `*_test.mio` outside excluded directories and publish a new release. |
| `mio test found no tests to run` | The test file has no `test_` function with the required signature. |
| `BUNKO_E_ARCHIVE ... file type is not allowed` | Remove scripts, binaries and other files; only `.mio`, `.md`, `.txt` and license files are allowed. |
| `BUNKO_E_ARCHIVE ... executable or setuid file mode` | Package files with mode `0644`. |
| `BUNKO_E_ARCHIVE ... symlink entries are not allowed` | Replace links with real files. |
| `BUNKO_E_SOURCE_VERIFY ... release is not immutable` | Enable immutable releases upstream and publish a new release. |
| `BUNKO_E_SOURCE_VERIFY ... does not resolve to source_commit` | The release tag points at another commit. Fix `source_commit` in a new record or release. |
| `BUNKO_E_SOURCE_VERIFY ... SHA-256` | Recompute the digest over the exact asset bytes. |
| `BUNKO_E_PLATFORM_DEPENDENCY` | A dependency lacks a declared target. Declare fewer targets or depend on a version that supports them. |
| `BUNKO_E_TEST_COMPILER` | `[mio]` excludes the compiler Bunko tests with. Widen `[mio]` or fix the source for Mio 2.1.0. |
| `compile error in <package>: ...` | Mio's own diagnostic. Reproduce it locally as described below. Imports of other packages must name a dependency declared in `[dependencies]` as `<namespace>/<package>`. |

Reproduce locally by downloading the Mio v2.1.0 release for your platform, laying
out the package and its dependencies under its `bunko/` directory, and running
`MIO_BUNKO=<mio>/bunko mio test <namespace>/<package>/... --format=json` from that
directory.
