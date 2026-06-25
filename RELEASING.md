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

## Maintainer promotion model

Maintainers promote releases by moving channel branches. Immutable tags answer
"what exact code is this?" Channel branches answer "what should a Computer get
when it asks for this channel?"

- `channels/lts` is the default for new Computers. Move it slowly, only to a
  final `vX.Y.Z` release that should be the conservative default.
- `channels/latest` is the fast-moving final release channel. Move it only to a
  final `vX.Y.Z` release, never to a dev or RC tag.
- Additional channels use the same branch shape: `channels/<name>`. Keep names
  lowercase, short, and operator-facing, for example `channels/beta` or
  `channels/customer-a`. They should still point at final releases unless the
  channel's maintainer note explicitly says it is allowed to track RCs.
- Dev releases and RCs are selectable by exact tag for testing. They must not
  become `latest`, `lts`, or any default Computer creation channel.

Promotion is a two-part operation:

1. Set or verify the GitHub release marker for the immutable tag.
2. Move the channel branch to the same immutable tag with `--force-with-lease`.

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
python3 scripts/publish_dev_release.py --base vX.Y.Z --suffix smoke --publish
```

Use this directly on a PR branch when you need to test a runtime change from
GitHub before the PR is merged. The script tags the selected commit, pushes the
tag, creates a GitHub Pre-release with Latest off, and prints the exact
`release_ref` for Hat admin's Custom/dev Computer creation flow.

The same flow is also available through the manual `dev-release` GitHub Actions
workflow once this workflow exists on the default branch. If the tag is created
by other release automation, keep the GitHub marker flags the same: Pre-release
on, Latest off, no channel branch movement.

Promote a final release to latest:

```bash
TAG=vX.Y.Z
git fetch origin main --tags
gh release view "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --json tagName,name,isPrerelease,isDraft
gh release edit "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --latest \
  --prerelease=false \
  --draft=false
git checkout -B channels/latest "$TAG"
git push origin channels/latest --force-with-lease
```

Promote a final release to LTS:

```bash
TAG=vX.Y.Z
git fetch origin main --tags
gh release view "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --json tagName,name,isPrerelease,isDraft,isLatest
git checkout -B channels/lts "$TAG"
git push origin channels/lts --force-with-lease
```

Promote a final release to another channel:

```bash
TAG=vX.Y.Z
CHANNEL=beta
git fetch origin main --tags
gh release view "$TAG" \
  --repo tinyloophub/tinyhat--runtimes--hermes \
  --json tagName,name,isPrerelease,isDraft,isLatest
git checkout -B "channels/$CHANNEL" "$TAG"
git push origin "channels/$CHANNEL" --force-with-lease
```

For non-`latest` channels, do not change the GitHub Latest marker unless the
same tag is also being promoted to `channels/latest`.

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

Then verify the channel branch points at the same commit as the tag:

```bash
TAG=vX.Y.Z
CHANNEL=lts
git fetch origin --tags "refs/heads/channels/$CHANNEL:refs/remotes/origin/channels/$CHANNEL"
test "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse "origin/channels/$CHANNEL^{commit}")"
```

## Upstream Hermes Agent

This repo does not publish Hermes Agent. Treat upstream Hermes versions and
installer behavior as external dependencies that this runtime prepares and
supervises through public, documented interfaces.
