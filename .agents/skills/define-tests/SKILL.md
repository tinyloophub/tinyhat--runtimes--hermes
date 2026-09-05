---
name: define-tests
description: Pick the right verification set for changes in the public Tinyhat Hermes runtime repo.
---

# define-tests - Hermes runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).
Use this repo-specific matrix for actual commands.

## Matrix

| Change | Minimum checks |
| --- | --- |
| Markdown/guidance/dev skills only | `git diff --check`; `python3 scripts/check_dev_skills.py`; `python3 scripts/check_repo_basics.py` |
| CI or validator scripts | Above plus `python -m compileall -q scripts` |
| Bootstrap/install scripts | Above plus shell syntax checks for each touched script |
| Python runtime code | Above plus `python -m compileall -q hermes_runtime`; focused unit tests for the public interface being changed; `python -m unittest discover -s tests -v` before a PR |
| Upstream Hermes install/config behavior | Above plus a Linux/container smoke that proves the documented Hermes interface still works |
| Release/version files | Relevant checks above plus review `CHANGELOG.md` and `VERSION` together |

Report exactly what ran.
If Docker or a Linux smoke is unavailable for runtime behavior, say that explicitly and name the runtime surface left unverified.
Never paste device codes or secrets into PRs or logs.
