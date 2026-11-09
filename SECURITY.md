# Reporting a security issue

Use the repository's
[private vulnerability reporting form](https://github.com/mio-kobo/bunko-index/security/advisories/new)
if available. Otherwise contact a Mio Kobo maintainer privately through an
established project channel. Please include the exact package name and version,
the upstream commit, and enough information to reproduce the issue. Keep exploit
details and credentials out of public issues.

Package tests run untrusted package code only on GitHub-hosted runners, from a
`pull_request` workflow with a read-only token and no secrets, using tooling
from the base branch. Report any way to escape that model, to read credentials
from a test, or to run package code on other hardware. See
[testing packages](docs/testing.md#security-model).

Maintainers should enable private vulnerability reporting on the repository.
Use the revocation process in [CONTRIBUTING.md](CONTRIBUTING.md) to record
published versions that must be blocked.
