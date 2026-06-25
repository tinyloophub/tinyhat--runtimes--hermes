# Tinyhat Hermes runtime

This repository is the public Tinyhat runtime for Hermes Computers. Its job is
small on purpose: keep a platform heartbeat alive, run a tiny whitelist of
platform commands, and stage runtime updates so they activate only on restart.

Canonical handle:

```text
tinyhat/runtimes/hermes
```

GitHub repository:

```text
tinyloophub/tinyhat--runtimes--hermes
```

The platform creates and heals this repository from the monorepo recovery seed
so dev and production use the same public runtime repo name.

## Runtime responsibilities

The runtime is intentionally not the agent framework. Hermes runs beside it.
This process only does the platform-visible work needed to manage a Computer:

- send a heartbeat to the Tinyhat platform;
- receive at most one command per heartbeat;
- run only the commands documented below;
- report command results back to the platform;
- stage updates and activate them on runtime restart.

For local development this runs in Docker with `--restart unless-stopped`. On a
production Linux Computer the same runtime should run under a process manager
such as systemd with restart enabled and a high enough priority that Hermes can
use the machine's resources without starving the heartbeat process.

## Authentication model

Production Hermes Computers should not store a long-lived Tinyhat platform
token. The production path is Google Cloud VM identity attestation: the runtime
fetches a Google-signed VM identity token from the metadata server when calling
Tinyhat, and the platform verifies that the token belongs to the expected
project, zone, and instance id before accepting the request.

The current `v0.0.1` foundation is local-development only. A local Docker
container does not have GCE metadata identity, so the Hat admin launch service
mints a scoped `TINYHAT_LOCAL_DEV_TOKEN` for `/hapi/v1/computers/local-dev/*`.
That local bearer secret is a dev harness substitute for attestation, not the
production authentication model.

## How a Computer is set up

The installer is intentionally a regular public shell script. You can read
[`install.sh`](install.sh) before running it, pin it to an exact tag, or run it
from `channels/lts` when you want the conservative default.

The foundation installer does this:

1. Downloads this repo at the requested ref, unless `--source-dir` points at a
   local checkout.
2. Copies the runtime Python package into `/opt/tinyhat-hermes-runtime`.
3. Writes the launcher to `/opt/tinyhat-hermes-runtime/bin/tinyhat-hermes-runtime`.
4. Writes runtime state under `/var/lib/tinyhat-hermes-runtime`.
5. Records the installed ref in `current/VERSION` and, when it can resolve one,
   the installed commit in `current/COMMIT_SHA`.
6. Writes a private env file at `/opt/tinyhat-hermes-runtime/env/runtime.env`.
   With systemd, the same env is copied to `/etc/tinyhat/hermes-runtime.env`.
7. When systemd is enabled, installs a service named
   `tinyhat-hermes-runtime.service`.

This foundation does **not** create Unix users yet and does **not** install
upstream Hermes Agent yet. In the current systemd path, the service is installed
by root and managed by systemd. When Hermes Agent installation is added, this
section should be updated in the same PR that creates or changes any service
user.

You can verify a machine setup with:

```bash
systemctl cat tinyhat-hermes-runtime.service
systemctl show tinyhat-hermes-runtime.service -p Restart -p Nice -p OOMScoreAdjust
sudo ls -la /opt/tinyhat-hermes-runtime /var/lib/tinyhat-hermes-runtime
sudo cat /opt/tinyhat-hermes-runtime/INSTALL_REF
sudo cat /var/lib/tinyhat-hermes-runtime/current/VERSION
sudo cat /var/lib/tinyhat-hermes-runtime/current/COMMIT_SHA
```

The env files contain platform connection data, such as the platform URL and
Computer id, so they are written with `0600` permissions. In the local Docker
harness they also contain the dev-only `TINYHAT_LOCAL_DEV_TOKEN` described
above. Do not paste env files into issues, logs, or support threads.

## Heartbeat protection

The runtime is the small process that keeps the platform able to reach the
Computer. Hermes Agent may use the rest of the machine, but the heartbeat should
survive ordinary load spikes.

The systemd service uses:

- `Restart=always` and `RestartSec=2`, so systemd starts it again if it exits.
- `Nice=-5`, so the scheduler gives it a little more priority than normal work.
- `OOMScoreAdjust=-900`, so Linux strongly prefers not to kill it when memory is
  tight.

These settings protect the heartbeat without putting artificial CPU or memory
caps on Hermes Agent. If the Computer needs more room for Hermes, the intended
fix is to resize the machine, not to silently starve the runtime or the agent.

## Transparency and trust layer

The runtime only accepts a small command whitelist. Each command is a normal file
under [`hermes_runtime/commands/`](hermes_runtime/commands/), and every command
must appear in the table below. If the platform sends a command that is not in
the whitelist, the runtime rejects it.

This is the trust layer: users and maintainers can inspect the exact code that
the platform is allowed to run on a Computer.

To verify that claim:

```bash
ls hermes_runtime/commands
sed -n '1,120p' hermes_runtime/commands/__init__.py
python3 scripts/check_repo_basics.py
```

`check_repo_basics.py` fails if a command is registered in code but missing from
the README table. That keeps the public explanation and the executable
whitelist tied together.

## Command whitelist

Every platform command is implemented as a file under
`hermes_runtime/commands/`. If a command is not listed here, the runtime rejects
it.

| Command | File | Why it exists | Side effects |
| --- | --- | --- | --- |
| `ping` | `hermes_runtime/commands/ping.py` | Basic liveness check from Hat admin. | None. Returns `pong`. |
| `whoami` | `hermes_runtime/commands/whoami.py` | Asks the platform to attest which Computer this runtime identity belongs to. | None. Calls `/hapi/v1/computers/local-dev/whoami`; the platform resolves the Computer from the local-dev bearer token. |
| `check_update` | `hermes_runtime/commands/check_update.py` | Checks the configured runtime update target on demand without waiting for the daily schedule. | Resolves the target ref in production, uses a platform-supplied ref directly in local dev, writes `updates/last_check.json`, reports the result to the platform update-check API, does not stage or activate code. |
| `stage_update` | `hermes_runtime/commands/stage_update.py` | Downloads or prepares a target runtime version without changing the running process. In the local foundation it writes a staged version marker. | Writes `staged/VERSION` under runtime state. |
| `activate_update` | `hermes_runtime/commands/activate_update.py` | Requests activation of an already staged update. | Writes `ACTIVATE_ON_RESTART` and exits after reporting success so the process manager restarts the runtime. |

## Daily update checks

The runtime does not check GitHub for updates on every heartbeat. Each heartbeat
only performs a fast local due-time check against small config files. When the
configured time has arrived and the check has not already run for that local
date, the runtime starts the network check as an async background task so the
heartbeat can continue.

Default schedule:

```text
02:35 America/Los_Angeles
```

Override the schedule by writing these files under the runtime state directory:

| File | Example | Meaning |
| --- | --- | --- |
| `config/update_check_time` | `02:35` | Local 24-hour time for the daily check. |
| `config/update_check_timezone` | `America/Los_Angeles` | IANA timezone for interpreting the check time. |
| `config/update_check_channel` | `lts` | `lts`, `latest`, or `custom`. |
| `config/update_check_ref` | `v0.20.0-dev.20260625T125559Z.pr2-smoke` | Optional exact ref. Required for custom checks. |

The latest check result is stored at `updates/last_check.json` for local
debugging and is reported through the platform update-check result API. It is
not embedded into heartbeat metrics. Use the admin `check_update` command when
you want to run the same check immediately from Hat admin.

In the local Docker harness, the platform supplies the target ref and the
runtime only compares that ref with the installed ref. It does not need
anonymous GitHub API access. Production update target resolution should use the
platform's attested machine identity path instead of this local shortcut.

The installer records the installed runtime ref in `current/VERSION` and, when
available, the resolved commit sha in `current/COMMIT_SHA`. Update checks compare
the target commit against that local commit before reporting an update.

## Update channels

Versions should be immutable Git tags shaped like `vX.Y.Z`.

- `latest`: the `channels/latest` branch points at the final release currently
  promoted for fast-moving Computers.
- `lts`: the `channels/lts` branch points at the conservative final release
  used by default Computer creation.
- `custom`: an explicit tag or commit selected by an operator.

For development, secondary test releases use prerelease tags shaped
`vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]`, for example
`v0.20.0-dev.20260625T173000Z.smoke`. They should be published as GitHub
pre-releases with the Latest marker off. Dev releases may point at a PR branch
commit, so you can test from GitHub without waiting for the PR to merge.

The helper below prints a tag in that shape and can optionally create an
annotated local tag:

```bash
python3 scripts/make_dev_release_tag.py --base v0.20.0 --suffix smoke
python3 scripts/make_dev_release_tag.py --base v0.20.0 --suffix smoke --apply
```

To publish the current branch as a testable GitHub prerelease, run:

```bash
python3 scripts/publish_dev_release.py --base v0.20.0 --suffix smoke --publish
```

The script prints `release_ref=<tag>` and an exact installer command. Paste that
tag into Hat admin's Custom/dev release field when creating or updating a local
Hermes Computer.

The runtime does not decide which tag is safe. It asks the platform for a target
version/channel, stages that target, and only activates it after restart. This
keeps a running agent from changing underneath active work.

See [VERSIONING.md](VERSIONING.md) for the full dev -> candidate -> final ->
latest/LTS lifecycle.

## Install path

Tinyhat installs this runtime the same way Hermes Agent documents its own
installer: with a public script that can be read before running it.

```bash
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/lts/install.sh \
  | bash -s -- --ref channels/lts
```

Use `channels/latest` for the latest promoted final, or an exact immutable tag
such as `v0.20.0`.

## Upstream Hermes install path

Hermes Agent's Linux command-line installer is:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

The installer expects Git plus `curl` and `xz-utils` on Linux. It handles the
Hermes clone, Python 3.11 via uv, Node.js 22, ripgrep, ffmpeg, a virtual
environment, and the global `hermes` command setup.

For a dedicated unprivileged service account, install Chromium system
libraries separately when browser automation is needed:

```bash
sudo npx playwright install-deps chromium
```

Then run the regular Hermes installer as the service user. A headless runtime
that does not need browser automation can pass `--skip-browser` to the
installer.

## Current scope

This foundation does not install Hermes Agent yet and does not assign a
Computer to an agent. It proves the management loop first: heartbeat,
attestation, command dispatch, and restart-activated updates.

## Development

Run the repository basics checks before opening a pull request:

```bash
git diff --check
python -m compileall -q scripts
python -m compileall -q hermes_runtime
python -m unittest discover -s tests -v
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
```
