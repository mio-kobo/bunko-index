# Package metadata v1

A release is one regular, non-executable UTF-8 TOML file of up to 64 KiB.
Only the fields below are accepted. Every field is required, including the
two tables. The [JSON Schema](../schemas/package.schema.json) describes the
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
| `dependencies` | Table | Map from quoted package names to exact version strings. An empty table declares zero dependencies. |

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

## Validation and project dependency closure

The path and metadata must identify the same package and version. Published
record bytes and Git file modes must match the base branch. Use a new version
to correct a record. CI uses the Git tree so renames, deletions, symlinks, and
file mode changes cannot be hidden by the working copy.

Errors use `BUNKO_E_*` codes and return a nonzero exit status. A
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
