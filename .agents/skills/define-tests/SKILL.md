---
name: define-tests
description: Pick the right verification set for changes in the public Tinyhat Hermes runtime repo.
---

# define-tests - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, skim the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Use this repo-specific matrix for actual commands.

## Matrix

| Change | Minimum checks |
| --- | --- |
| Markdown/guidance/dev skills only | `git diff --check`; `python3 scripts/check_dev_skills.py`; `python3 scripts/check_repo_basics.py` |
| CI or validator scripts | Above plus `python -m compileall -q scripts` |
| Future bootstrap/install scripts | Above plus shell syntax checks for each touched script |
| Future Python runtime code | Above plus focused unit tests for the public runtime interface being changed |
| Upstream Hermes install/config behavior | Above plus a Linux/container smoke that proves the documented Hermes interface still works |
| Release/version files | Relevant checks above plus review `CHANGELOG.md` and `VERSION` together |

Report exactly what ran.
If Docker or a Linux smoke is unavailable for runtime behavior, say that explicitly and name the runtime surface left unverified.
Never paste device codes or secrets into PRs or logs.
