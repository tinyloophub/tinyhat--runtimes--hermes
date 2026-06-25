# Releasing the Tinyhat Hermes runtime

This repository publishes the public runtime slot for Tinyhat-managed Hermes
Computers. It versions independently from Tinyloop, OpenClaw, and upstream
Hermes Agent.

## Release shapes

- Final releases use tags shaped `vX.Y.Z`.
- Release candidates use tags shaped `vX.Y.Z-rc.N`.
- Secondary development releases use tags shaped
  `vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]`.
- Channel branches use `channels/latest` and `channels/lts`; they point at
  final release commits and are the only moving refs Computer creation should
  use by default.
- The GitHub release title must equal the tag exactly.
- Release summaries belong in the release body or `CHANGELOG.md`, not in the
  title.
- Mark GitHub **Pre-release** exactly when the tag is `vX.Y.Z-rc.N` or
  `vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]`.
- Mark GitHub **Latest** only on the final release being promoted, never on a
  release candidate or secondary development release.
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

Secondary development release:

```bash
TAG=$(python3 scripts/make_dev_release_tag.py --base vX.Y.Z --suffix smoke --apply)
git push origin "$TAG"
gh release create "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --title "$TAG" \
  --prerelease \
  --latest=false \
  --verify-tag \
  --notes "Secondary dev runtime release for local Computer testing."
```

If the tag is created by release automation, keep the `gh release create`
marker flags the same.

Promote a final release to latest:

```bash
TAG=vX.Y.Z
git fetch origin main --tags
git checkout -B channels/latest "$TAG"
git push origin channels/latest --force-with-lease
```

Promote a final release to LTS:

```bash
TAG=vX.Y.Z
git fetch origin main --tags
git checkout -B channels/lts "$TAG"
git push origin channels/lts --force-with-lease
```

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
- `isPrerelease` is `true` for `vX.Y.Z-rc.N` and secondary dev releases,
  and `false` for `vX.Y.Z`.
- `isLatest` is `false` for candidates and secondary dev releases, and `true`
  for the final promotion cut.
- `channels/latest` points at the promoted latest final release commit.
- `channels/lts` points at the conservative LTS final release commit.

## Upstream Hermes Agent

This repo does not publish Hermes Agent. Treat upstream Hermes versions and
installer behavior as external dependencies that this runtime prepares and
supervises through public, documented interfaces.
