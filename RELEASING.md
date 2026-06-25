# Releasing the Tinyhat Hermes runtime

This repository publishes the public runtime slot for Tinyhat-managed Hermes
Computers. It versions independently from Tinyloop, OpenClaw, and upstream
Hermes Agent.

## Release shapes

- Final releases use tags shaped `vX.Y.Z`.
- Release candidates use tags shaped `vX.Y.Z-rc.N`.
- The GitHub release title must equal the tag exactly.
- Release summaries belong in the release body or `CHANGELOG.md`, not in the
  title.
- Mark GitHub **Pre-release** exactly when the tag is `vX.Y.Z-rc.N`.
- Mark GitHub **Latest** only on the final release being promoted, never on a
  release candidate.
- Published tags are immutable. Fix naming drift by editing the GitHub release
  title or marker flags, not by deleting or rewriting tags.

## Commands

Final release:

```bash
TAG=vX.Y.Z
gh release create "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --title "$TAG" \
  --latest \
  --verify-tag \
  --notes-file CHANGELOG.md
```

Release candidate:

```bash
TAG=vX.Y.Z-rc.N
gh release create "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --title "$TAG" \
  --prerelease \
  --latest=false \
  --verify-tag \
  --notes-file CHANGELOG.md
```

If the tag is created by release automation, keep the `gh release create`
marker flags the same.

## Conformance check

Before treating a release as done, inspect its marker payload:

```bash
gh release list \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --limit 500 \
  --json tagName,name,isPrerelease,isLatest,isDraft \
  --jq "map(select(.tagName == \"$TAG\")) | .[0]"
```

Expected:

- `tagName` equals `name`.
- `isDraft` is `false`.
- `isPrerelease` is `true` for `vX.Y.Z-rc.N` and `false` for `vX.Y.Z`.
- `isLatest` is `false` for candidates and `true` for the final promotion cut.

## Upstream Hermes Agent

This repo does not publish Hermes Agent. Treat upstream Hermes versions and
installer behavior as external dependencies that this runtime prepares and
supervises through public, documented interfaces.
