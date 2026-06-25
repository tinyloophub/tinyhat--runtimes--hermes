---
name: release
description: Cut or verify a release of the public Tinyhat Hermes runtime repo.
---

# release - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, skim the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
This repo releases the Tinyhat Hermes runtime package itself.

## Before Release

- Confirm `VERSION` and `CHANGELOG.md` match the intended runtime behavior.
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

- Tags use `vX.Y.Z`.
- The GitHub release notes should be public-safe and should name any required companion Tinyloop monorepo or upstream Hermes Agent PRs.
- Do not publish a runtime that requires unavailable upstream Hermes behavior unless the release notes call out the dependency.
