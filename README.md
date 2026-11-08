# Bunko Package Index

Bunko (文庫) is the official package manager for the Mio programming language,
available through the compiler as `mio bunko`. This repository is the official
package catalog. It defines the metadata, naming, dependency, and review rules
used to admit Mio source packages.

Each release is a UTF-8 TOML file at
`packages/<namespace>/<package>/<version>.toml`. The index opens empty, and the
first accepted package adds the `packages/` tree. The metadata includes publisher
identity, an upstream source commit and immutable release asset, SHA-256, license,
compiler compatibility, and exact dependencies. See the
[package format](docs/package-format.md) for a field reference.

Community packages use `publisher/package`. The publisher portion is the lowercase
GitHub account or organization that owns the source repository. `kiso/*` is
reserved for the standard library, `mio/*` for official extensions, and `bunko/*`
for future official use. Similar names in the publisher portion may receive a
manual review warning.

To submit a new package, prepare the source archive upstream, add its TOML file,
and open a pull request. To publish an update, add a file for the new version.
Once accepted, a version and its metadata are permanent. An unsafe release is
revoked through a separate policy file. CI checks the proposed index and
maintainers verify publisher authority, provenance, archive contents, and
naming warnings before approving. See the [contribution guide](CONTRIBUTING.md).

Bunko v1 accepts Mio source packages. Install scripts, arbitrary build scripts,
and native binaries are prohibited. Projects declare exact direct dependencies
and use a shared global cache without a lock file. Each project can use one
version per package. Compilation reports missing dependencies for the user to
install explicitly.

Run the checks from this repository with Python 3.11 or newer and Git. There
are no Python packages to install.

```sh
python3 tools/validate.py
python3 tools/validate.py --base origin/main
python3 -m unittest discover -s tests -v
```

The [third-party index policy](docs/third-party-indexes.md) describes the rules
for admission to the official index and the intended boundaries for future taps.
