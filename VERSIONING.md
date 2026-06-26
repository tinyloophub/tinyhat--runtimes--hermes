# Tinyhat Hermes runtime versioning

This repo has two separate ideas:

- **Immutable release tags** identify exact code.
- **Channel branches** identify what the platform should install by default.

Do not move SemVer tags after publishing them. Move channel branches when a
release is promoted.

## Release lifecycle

| Phase | Tag shape | GitHub marker | Channel branch | Purpose |
| --- | --- | --- | --- | --- |
| Development | `vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]` | Pre-release, not Latest | none | Fast secondary releases for local Computer testing. |
| Candidate | `vX.Y.Z-rc.N` | Pre-release, not Latest | none | Reviewable promotion candidate after the dev loop is stable. |
| Final | `vX.Y.Z` | Release; Latest only when promoted | optional `channels/latest` | Stable artifact that can be selected exactly. |
| LTS | existing final `vX.Y.Z` | Release | `channels/lts` | Conservative default for new Computers. |

## Installer refs

Use these raw installer refs from Computer creation flows:

```bash
# LTS default
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/lts/install.sh \
  | bash -s -- --ref channels/lts

# Latest final release
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/latest/install.sh \
  | bash -s -- --ref channels/latest

# Exact immutable release
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/vX.Y.Z/install.sh \
  | bash -s -- --ref vX.Y.Z
```

For development, generate an identifiable secondary release tag:

```bash
python3 scripts/make_dev_release_tag.py --base v0.20.0 --suffix smoke
```

To test code from a PR branch before the PR branch is merged, publish a dev
prerelease from that branch:

```bash
python3 scripts/publish_dev_release.py --base v0.20.0 --suffix smoke --publish
```

The script tags the selected commit, pushes the tag, creates a GitHub
Pre-release with Latest off, and prints the exact `release_ref` to use in the
Hat admin Custom/dev Computer creation flow. The exact dev tag installer shape
is:

```bash
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/vX.Y.Z-dev.YYYYMMDDTHHMMSSZ.suffix/install.sh \
  | bash -s -- --ref vX.Y.Z-dev.YYYYMMDDTHHMMSSZ.suffix
```

## Promotion rules

1. Dev tags may be created often from any committed branch, including an open
   PR branch before the PR branch is merged. Publish them as GitHub
   pre-releases with Latest off. They never move `channels/latest` or
   `channels/lts`.
2. RC tags are for promotion review. Publish them as GitHub pre-releases with
   Latest off. They never move channel branches.
3. Final tags are immutable. If the final is the active default, update the
   GitHub Latest marker and move `channels/latest` to the same commit.
4. LTS is an operator decision. Move `channels/lts` only after the final release
   has enough confidence to become the conservative default.
5. Additional channel branches must use `channels/<name>`, where `<name>` is a
   short lowercase operator-facing name. Document why the channel exists before
   making it a Computer creation option.

Promotion is maintainer-only. Agents must not open promotion PRs for
`channels/latest` or `channels/lts`, and agents must not start cross-agent
review for release or promotion PRs. To promote from the command line, switch
`gh` to `farid-tinyloop` and run:

```bash
python3 scripts/promote_release_channel.py --tag vX.Y.Z --channel latest,lts
```

To promote from GitHub's UI, run the `promote-release-channel` workflow. The
workflow only runs for `farid-tinyloop` and requires
`MAINTAINER_PROMOTION_TOKEN`, a token owned by `farid-tinyloop`.

## Channel refs and update checks

Channel branches are moving installer selectors. They are not, by themselves,
proof that a Computer should update. This matters when branch protection is on:
`channels/lts` and `channels/latest` may point at merge commits that contain a
final release tag instead of pointing at the tag commit exactly.

The platform should resolve a channel to the concrete final tag it contains and
send that tag to `check_update`, for example:

```json
{"channel": "lts", "target_ref": "v0.0.7"}
```

If a runtime receives only `{"channel": "lts", "target_ref": "channels/lts"}`,
it may confirm the selector is eligible, but it must not report
`update_available=true`. That keeps a Computer already running `v0.0.7` from
treating the protected `channels/lts` merge commit as a newer version. The
platform should send the concrete final tag when it wants a version decision.

Channel branch update example:

```bash
TAG=vX.Y.Z
CHANNEL=latest
gh auth switch --user farid-tinyloop
python3 scripts/promote_release_channel.py --tag "$TAG" --channel "$CHANNEL"
```

The helper marks the GitHub release as Latest when `latest` is one of the
requested channels. For `channels/lts` or any other channel, it leaves the
GitHub Latest marker alone unless the same tag is also being promoted to
`channels/latest`.
