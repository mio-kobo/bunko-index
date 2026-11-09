## Change

List each exact package version and link its upstream source commit. For an index
maintenance change, describe the policy or tooling affected.

## Package submissions

- [ ] I control the publisher account or have linked evidence of delegation.
- [ ] Each release adds one metadata file and uses exact dependency versions.
- [ ] I verified the SHA-256 of the source archive at the pinned URL.
- [ ] The archive contains Mio source and ordinary documentation or license files.
- [ ] `[platforms]` lists only targets I support, and every dependency supports them.
- [ ] The archive contains at least one `*_test.mio` file.
- [ ] I ran the index validator locally.

Maintainers should verify publisher authorization, source and license, and
investigate any naming warnings before approving.
