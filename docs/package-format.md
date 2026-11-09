# Package metadata v1

A release is one regular, non-executable UTF-8 TOML file of up to 64 KiB.
Only the fields below are accepted. Every field is required, including the
three tables. The [JSON Schema](../schemas/package.schema.json) describes the
data after TOML parsing. The validator also checks file paths, ownership rules,
and relationships between fields and records.

| Field | TOML type | Rule |
| --- | --- | --- |
| `name` | String | Exact `namespace/package`. Both segments use 1–39 lowercase ASCII letters or digits, with single hyphens allowed between groups. |
| `version` | String | Exact `MAJOR.MINOR.PATCH`. Each component is 1–9 digits with no leading zeros, except `0`. V1 supports the stable numeric subset of SemVer. |
| `publisher` | String | `github:<lowercase-login>`. Community namespaces match the login. Official namespaces use the policy's publisher allowlist. |
| `description` | String | A nonblank, single-line description, up to 240 characters. |
| `repository` | String | Canonical `https://github.com/<owner>/<repo>` URL owned by the publisher. Use lowercase for the owner. |
| `source_commit` | String | Full 40-character lowercase Git commit SHA corresponding to the source archive. |
| `release_id` | String | Positive GitHub release ID in decimal, without leading zeros. |
| `archive` | String | `https://api.github.com/repos/<owner>/<repo>/releases/assets/<asset-id>` for an uploaded `.tar.gz` asset on the published immutable release. Use the same owner and repo as `repository`. |
| `sha256` | String | Exactly 64 lowercase hexadecimal digits, computed over the archive bytes. |
| `license` | String | One SPDX license identifier such as `MIT` or `Apache-2.0`. CI checks token syntax. Reviewers check the identifier and actual terms. |
| `mio` | Table | `min` is the inclusive compiler version and `before` the exclusive upper bound. Both use the numeric version format and `min` must precede `before`. |
| `platforms` | Table | Maps each supported operating system to a nonempty array of supported CPU architectures. See [Platforms](#platforms). |
| `dependencies` | Table | Map from quoted package names to exact version strings. An empty table declares zero dependencies. |

## Platforms

```toml
[platforms]
linux = ["x86_64"]
macos = ["arm64"]
windows = ["x86_64"]
```

`[platforms]` is required and declares exactly the targets the release supports.
A release that works on one target declares one:

```toml
[platforms]
linux = ["x86_64"]
```

| Rule | Result |
| --- | --- |
| Operating system keys | Only `linux`, `macos` and `windows`. `darwin`, `win32`, `*` and other keys are errors. |
| Architecture values | Only the canonical `x86_64` and `arm64`. `amd64`, `x64`, `aarch64`, `*` and other spellings are errors. Values are case-sensitive. |
| Missing table or no keys | Error. There is no default platform. |
| Empty array, duplicate member, non-array value | Error. |
| Operating system not listed | **Unsupported.** |
| Architecture not listed under a listed operating system | **Unsupported.** |

A target that is not declared is unsupported even if the source would compile
there. Bunko does not detect platforms, expand wildcards or infer defaults, and
a future installer must refuse any target the record does not list.

Only targets with an official Mio compiler and a compatible GitHub-hosted test
runner can be declared. For Mio v2.1.0 these are `linux/x86_64`,
`macos/arm64` and `windows/x86_64`, listed with their runners and pinned
compiler downloads in [`policies/platform-targets.toml`](../policies/platform-targets.toml).
A well-formed pair outside this set, such as `linux = ["arm64"]`, fails with
`BUNKO_E_PLATFORM`. Targets are added to that file only after verifying the
official compiler and a trusted runner.

### Compiler version versus platform

`[mio]` and `[platforms]` are independent. `[mio]` says which compiler versions
accept the source. `[platforms]` says which operating systems and architectures
the release supports. A release must satisfy both. CI tests each declared target
with the Mio version pinned for it, so a `[mio]` range that excludes that
version fails the test plan (`BUNKO_E_TEST_COMPILER`) rather than being skipped.

### Dependency closure

A release is installable on a target only if every release in its exact-version
dependency closure also declares that target. The validator therefore rejects a
release that declares a target any dependency lacks, and names the chain:

```text
error: BUNKO_E_PLATFORM_DEPENDENCY 'packages/bob/mid/1.0.0.toml'
  carol/root@1.0.0 -> bob/mid@1.0.0 -> alice/leaf@1.0.0 does not declare macos/arm64 required by carol/root@1.0.0 (declares: linux/x86_64)
```

A dependency may support more targets than its dependents. With `--roots`,
each root's own targets are checked against what that root reaches.

A published record cannot be edited. To fix a gap, publish a new dependency
version with the target, or a new dependent version that declares fewer targets.

A complete example lives in [tests/fixtures/package.toml](../tests/fixtures/package.toml).
Its identifiers are for tests. A dependency entry under `[dependencies]` looks
like `"publisher/package" = "1.0.0"`. Select a published version for every entry.
Floating values such as `latest`, `^1.0.0`, and `~1.0.0` fail validation.

To retrieve the release record, use
`https://api.github.com/repos/<owner>/<repo>/releases/<release-id>`. The
release must report `immutable: true`, with the archive asset in its asset list.
Use `Accept: application/octet-stream` when downloading from the asset API and
verify the archive's SHA-256 before extraction. GitHub may stream the asset or
redirect to its download host. A missing asset or digest mismatch is an error.
The [GitHub release documentation](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
describes which fields GitHub protects after publication.

## Archive contents

The archive is the package directory itself: its paths are relative to the
package root with no wrapper directory. Bunko V1 accepts only regular,
non-executable UTF-8 text files named with letters, digits, `.`, `_` and `-`:
`*.mio`, `*.md`, `*.txt`, and `LICENSE`, `COPYING`, `NOTICE`, `AUTHORS` or
`README` (optionally with a `-suffix`). Symlinks, hard links, device files,
absolute or `..` paths, executable modes, native binaries, scripts, install
hooks, case-insensitive name collisions and Windows reserved names are rejected.
Archives may hold at most 2000 files, 8 MiB per file and 64 MiB in total. Mio
imports the package as `<namespace>/<package>`, so its source files belong at
the archive root. A wrapper directory such as `pkg-1.0.0/` is not stripped: its
files would sit one level deeper and could not be imported as the package.

## Tests

Every new package version must include at least one `*_test.mio` file. See
[testing](testing.md) for the conventions and the CI that runs them.

## Validation and project dependency closure

The path and metadata must identify the same package and version. Published
record bytes and Git file modes must match the base branch. Use a new version
to correct a record. CI uses the Git tree so renames, deletions, symlinks, and
file mode changes cannot be hidden by the working copy.

Errors use `BUNKO_E_*` codes and return a nonzero exit status. Platform errors
are `BUNKO_E_SCHEMA` (malformed `[platforms]`), `BUNKO_E_PLATFORM` (a target
Bunko cannot test), and `BUNKO_E_PLATFORM_DEPENDENCY` (a dependency lacks a
declared target). A
`BUNKO_W_MANUAL_REVIEW` warning appears when a namespace matches a configured
visual pattern or falls within the configured Damerau–Levenshtein distance of a
reserved namespace. The warning alone leaves the validator successful. Exact
reserved-name and publisher violations are errors.

The validator checks each usable release's dependency closure independently.
Separate releases can depend on different versions of a package. A project
that combines them must still have a single version. Pass the project's direct
dependencies to `--roots` to check them together.

```sh
python3 tools/validate.py --roots alice/web@1.0.0 carol/api@2.0.0
```

A conflict reports both paths. The index never chooses a newer version in place
of an exact requirement.
