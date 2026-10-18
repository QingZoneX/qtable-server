# Development source and public snapshots

`QingZoneX/QTableServerPrivate` is the sole canonical development source. `QingZoneX/qtable-server` receives explicitly reviewed release snapshots. Product implementation is not developed on two independent main branches.

Public issues and proposed patches are accepted as intake. A maintainer ports any product-code fix into the canonical source before including it in the next public snapshot. Public workflow, deployment defaults and documentation may have reviewed repository-specific overlays; they do not establish a second product implementation authority.

The Platform `scripts/public_release.py` preparation tool inventories an exact 40-character canonical commit and explicitly selected paths, with file hashes and modes. It exports only the reviewed inventory to a deterministic history-free archive with `SOURCE-PROVENANCE.json`. It does not push, tag, publish, copy Git history or modify a public checkout. Tool/source commits, inventory digest, archive digest, selected paths, removals, drift and overlay decisions belong in the public sync PR. Sensitive path filters supplement the owning secret/license scans and source review; they are not a content secret scanner.

Before applying a future archive, compare it against the prior public provenance and investigate source drift. Port source fixes first, explicitly review deletions and preserve reviewed public overlays. Run the public release/security/build gates against the exact exported source and the matching server/Web revision pair. Publication requires separate release authorization and real release-gate evidence. This documentation-only change does not synchronize private source, create a tag or certify an installed/production release.
