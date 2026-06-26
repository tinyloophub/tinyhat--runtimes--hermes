---
name: release
description: Cut or verify a release of the public Tinyhat Hermes runtime repo.
---

# release - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, skim the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
This repo releases the Tinyhat Hermes runtime package itself.

## Before Release

- Confirm `VERSION`, `hermes_runtime/__init__.py` `__version__`, and
  `CHANGELOG.md` match the intended runtime behavior. `VERSION` and
  `__version__` must stay identical so the `running_version` command proves the
  same code version the release tag advertises.
- Confirm the README.md command whitelist matches
  `hermes_runtime/commands/__init__.py`: every command must have one row, the
  row must name its file, why the platform needs it, and whether it has side
  effects. This is part of the public transparency contract for the runtime.
- Confirm the release commit is on `main` and includes only reviewed changes.
- Run:

  ```bash
  git diff --check
  python -m compileall -q scripts
  python3 scripts/check_dev_skills.py
  python3 scripts/check_repo_basics.py
  ```

- Add runtime-specific tests before release once this repo contains boot/install/launch code.

## Release Shape

- Tags use:
  - `vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]` for secondary development releases.
  - `vX.Y.Z-rc.N` for promotion candidates.
  - `vX.Y.Z` for final releases.
- GitHub Pre-release is on for dev and RC tags; Latest is off for both.
- Final releases may receive the GitHub Latest marker when promoted.
- `channels/latest` and `channels/lts` are movable branch refs used by
  Computer creation. They must point at final release commits, never dev or RC
  tags.
- The GitHub release notes should be public-safe and should name any required companion Tinyloop monorepo or upstream Hermes Agent PRs.
- Do not publish a runtime that requires unavailable upstream Hermes behavior unless the release notes call out the dependency.

## Lifecycle

1. Cut secondary dev releases freely while testing local Computers. For a PR
   branch that needs GitHub-backed testing before merge, run:

   ```bash
   python3 scripts/publish_dev_release.py --base vX.Y.Z --suffix smoke --publish
   ```

   Use the printed `release_ref` in Hat admin's Custom/dev Computer creation
   or update flow. Dev releases are GitHub Pre-releases, Latest off, and never
   move `channels/latest` or `channels/lts`.
2. Cut an RC once the dev loop is stable enough for promotion review.
3. Cut a final `vX.Y.Z` tag after review.
4. Move `channels/latest` to the final when it should be the fast-moving
   default. Before moving the branch, run `gh release edit "$TAG" --latest
   --prerelease=false --draft=false` so the GitHub marker matches the channel.
5. Move `channels/lts` only when that final should be the conservative default.
6. For any other channel, use `channels/<name>` with a short lowercase
   operator-facing name. Move it with the same `git checkout -B` and
   `git push origin ... --force-with-lease` shape, and do not change the GitHub
   Latest marker unless the same tag is also promoted to `channels/latest`.

Read `VERSIONING.md` before changing this flow.
