# Third-party packages and future indexes

A community package enters the official index through the same pull request
and maintainer review as other packages. Submit it under a verified publisher,
provide a public and immutable source archive and a license that permits
redistribution, and list every direct dependency at an exact version. All
dependencies of an official entry must resolve within the official index.
See the [contribution guide](../CONTRIBUTING.md) for the review steps.

Bunko may later support separately maintained indexes, similar to Homebrew taps.
The client should require users to opt in to a specific index, identify the
origin of each package, and keep the official namespaces protected across all
indexes. If two indexes claim the same package, the client should require an
explicit source choice. Cache entries should retain source identity and the
archive digest so one index cannot silently replace another's content.

Apply the same exact-version and single-version rules across the project graph.
Treat each external index's metadata and archives as untrusted input. Index
discovery, trust policy, signature verification, and cross-index resolution
belong to the future client contract.
