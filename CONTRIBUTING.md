# Contributing to the Bunko index

## Add a release

1. Prepare a public GitHub repository under the publisher identity. Pick a new
   version and the exact source commit. Package the Mio source as a
   `.tar.gz` asset with any license and documentation. Omit submodules, native
   binaries, symlinks, install hooks, and arbitrary build scripts.
2. Enable immutable releases upstream. Upload the archive to a draft release,
   associate the tag with the source commit, and publish the release. Record
   the release ID, asset ID, source commit, and SHA-256 of the downloaded bytes.
   Keep the upstream repository and asset available to users.
   Include at least one `*_test.mio` file that uses `kiso/testing`
   ([testing](docs/testing.md)). Package files must be non-executable text with
   portable names, and the archive root is the package directory.
3. Add `packages/<namespace>/<package>/<version>.toml` using the
   [package format](docs/package-format.md). A first submission and a new
   version both add a file. List every direct dependency with its exact index
   version. Include mutually dependent submissions only when the resulting
   graph is acyclic. Declare `[platforms]` with only the targets you want
   supported and CI can test:

   ```toml
   [platforms]
   linux = ["x86_64"]
   macos = ["arm64"]
   ```

   Anything you omit is unsupported. Every release in your dependency closure
   must support each target you declare.
4. Fetch the latest target branch. Run `python3 tools/validate.py --base origin/main`
   and open a pull request. Link the immutable release and source commit.
   If the PR account differs from the publisher, include evidence of delegation
   that maintainers can verify through a publisher-controlled channel.
5. Wait for **Validate Bunko index** and **Package tests**. Package tests create
   one job per declared target, on GitHub-hosted runners, using the Mio compiler
   pinned for the target. Fix failures by publishing a new release and a new
   version record; a published record never changes. Failures are explained in
   [testing: troubleshooting](docs/testing.md#troubleshooting).

## Review a release

Review ownership and delegation for every publisher, including publishers whose
names pass automated checks. Verify that the GitHub release is published,
reports `immutable: true`, and contains the exact asset ID. Resolve the release
tag to the stated commit and check that the archive matches that source,
contains only allowed files, and has the stated SHA-256. Confirm that its license
allows redistribution and that the compiler compatibility claim and direct
dependencies are reasonable. Record the disposition of naming warnings in the
PR review. Treat the archive as untrusted data during review. Check that each declared
platform is one the package really supports and that omitted ones are not
needed. Passing CI is evidence, not approval: review the source for malicious
behavior, because tests execute package code. Do not run it on private
hardware until the source review is complete.

Dedicated-hardware testing is a separate, manual maintainer step. It never
changes `[platforms]`, which can only list targets CI can test.

CI never approves, labels as reviewed, or merges a PR. To **reject**, request
changes with the failing check or policy reason and close the PR. To **approve**,
confirm provenance, delegation, naming warnings and passing checks, then approve
as a code owner. Review labels such as `review: naming` stay manual.

Reserved namespace submissions must come from an account ID on the base branch
allowlist. Merge any authorization change in a separate policy PR before using
the new authorization. The initial CODEOWNERS entry and reserved submission
allowlist use the verified maintainer account `ExMikuPro`. Add another eligible
maintainer as a code owner before requiring code owner approval for that account's
own PRs.

After bootstrapping, configure the default branch to require pull requests,
code owner approval, the **Validate Bunko index** check, and an up-to-date
branch. Dismiss stale approvals and disallow direct pushes, force pushes, and
deletions. CODEOWNERS comes from the base branch and protects changes to CI,
policy, tooling, and the ownership file itself once required reviews are enabled.

## Respond to an unsafe release

Keep the published record and add its exact `name@version` to
`policies/revoked-versions.toml`, with an HTTPS advisory link. In the same PR
revoke any published release whose dependency closure requires that version.
Publish new versions to fix affected dependencies. Packages that depend on the
revoked version fail the validator and the package tests until they are revoked
or replaced. The validator rejects a live
release that reaches a revoked one and retains the historical records for audit.

See [SECURITY.md](SECURITY.md) for private reporting.
