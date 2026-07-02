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

Local Docker creation still uses the same public installer surface. The
platform starts a plain Linux/Python container, passes local development
connection values as environment variables, and runs `install.sh` with
`--run-foreground`. The foreground restart loop lives in this repository's
public installer, not in a private platform-generated shell script.

## Authentication model

Production Hermes Computers should not store a long-lived Tinyhat platform
token. The production path is Google Cloud VM identity attestation: the runtime
fetches a Google-signed VM identity token from the metadata server when calling
Tinyhat, and the platform verifies that the token belongs to the expected
project, zone, and instance id before accepting the request.

Local Docker does not have GCE metadata identity, so the Hat admin launch
service mints a scoped `TINYHAT_LOCAL_DEV_TOKEN` for
`/hapi/v1/computers/local-dev/*`. That local bearer secret is only a dev harness
substitute for attestation. On GCloud Computers the runtime does not need a
Tinyhat platform token: when it calls the platform, it asks the Google metadata
server for a short-lived VM identity token, reuses that token until it is close
to expiry, and sends it to the existing `/hapi/v1/computers/me/*` platform APIs.
The platform verifies the Google token before accepting the call.

## How a Computer is set up

The installer is intentionally a regular public shell script. You can read
[`install.sh`](install.sh) before running it, pin it to an exact tag, or run it
from `channels/lts` when you want the conservative default.

The foundation installer does this:

1. Downloads this repo at the requested ref, unless `--source-dir` points at a
   local checkout.
2. Ensures the OpenAI Codex CLI is installed. If `codex` is already available,
   it leaves it alone; otherwise it installs the public npm package
   `@openai/codex`. On apt-based Linux machines without a recent node/npm
   pair, it installs Node.js from NodeSource's configured major line first
   (`TINYHAT_CODEX_NODE_MAJOR`, default `22`). Tinyhat installs Codex CLI
   during provisioning because Codex is the supported subscription-auth
   provider and `/codex_limits` needs `codex app-server` after the user
   connects auth. Unusual local development hosts that do not need Codex auth
   can set `TINYHAT_SKIP_CODEX_CLI=1` to skip this dependency.
3. Copies the runtime Python package and import-safe bootstrap into
   `/opt/tinyhat-hermes-runtime`.
4. Writes the launcher to `/opt/tinyhat-hermes-runtime/bin/tinyhat-hermes-runtime`.
5. Writes runtime state under `/var/lib/tinyhat-hermes-runtime`.
6. Records the installed ref in `current/VERSION` and, when it can resolve one,
   the installed commit in `current/COMMIT_SHA`.
7. Writes a private env file at `/opt/tinyhat-hermes-runtime/env/runtime.env`.
   With systemd, the same env is copied to `/etc/tinyhat/hermes-runtime.env`.
8. When `--run-foreground` is passed, runs the installed runtime in the
   foreground with a small restart loop for local Docker or another external
   supervisor.
9. When systemd is enabled, installs a service named
   `tinyhat-hermes-runtime.service`.

This foundation does **not** create Unix users yet. In the current systemd path,
the Tinyhat runtime service is installed by root and managed by systemd. Hermes
Agent installation is handled separately by the whitelisted `install_hermes`
runtime command, which uses the official Hermes installer after the Tinyhat
runtime is alive. The Tinyhat plugin is then installed by the separate
`install_tinyhat_plugin` command. That command resolves `channels/lts` in
`tinyhat-ai/tinyhat` by default, prepares that exact checkout, and hands it to
Hermes' public plugin CLI. Plugin setup stays visible, independently testable,
and channel-driven instead of accidentally following the plugin repo's default
branch.

You can verify a machine setup from Hat admin with the read-only
`setup_snapshot` command. On the machine itself, these local checks show the
same underlying facts:

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
above. GCloud Computers should not have a Tinyhat platform bearer token in these
files; they use metadata-server identity tokens at request time. By default the
identity-token audience is the platform URL; set `TINYHAT_COMPUTER_TOKEN_AUDIENCE`
only when the platform verifier is configured for a different audience. Do not
paste env files into issues, logs, or support threads.

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

## Heartbeat cadence

Heartbeat timing is intentionally simple and state-aware:

- before assignment, including `provisioning` and `ready`, the runtime checks in
  every 1 second so the platform can attach the Computer to an agent quickly;
- after assignment, `assigned` and `active` Computers check in every 10 seconds
  by default because the Computer is no longer sitting idle waiting to be used;
- `TINYHAT_HEARTBEAT_INTERVAL_SECONDS` is a fixed override for tests and unusual
  debugging sessions;
- `TINYHAT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS` and
  `TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS` can tune the two normal
  intervals without changing runtime code.

The runtime learns the platform state from the heartbeat response, so local
Docker and GCloud Computers use the same cadence rules.

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
| `whoami` | `hermes_runtime/commands/whoami.py` | Asks the platform to attest which Computer this runtime identity belongs to. | None. In the current local-development foundation it calls `/hapi/v1/computers/local-dev/whoami`, and the platform resolves the Computer from the scoped dev token. Production GCE Computers should use the VM identity attestation path instead, not the local-dev token path. |
| `check_update` | `hermes_runtime/commands/check_update.py` | Checks the configured runtime update target on demand without waiting for the daily schedule. | Resolves the target ref in production, uses a platform-supplied ref directly in local dev, writes `updates/last_check.json`, best-effort reports the result to the platform update-check API, does not stage or activate code. The result also includes `plugin_update_check`, which compares the installed Tinyhat plugin against its configured channel. LTS/latest runtime decisions require a concrete final tag such as `v0.0.7`; a raw channel selector like `channels/lts` is not enough evidence to report a runtime update as available. |
| `update_status` | `hermes_runtime/commands/update_status.py` | Shows the installed runtime version, any staged local update, startup activation errors, the last runtime update-check result, and the latest cached plugin update-check result. | Reads state files and the installed plugin manifest only. It does not contact GitHub or change code. |
| `running_version` | `hermes_runtime/commands/running_version.py` | Proves which runtime package version the currently running Python process imported. | Reads the already-imported `hermes_runtime` module object only. Does not read or write runtime state metadata. |
| `recent_commands` | `hermes_runtime/commands/recent_commands.py` | Shows the local command ledger from the Computer. | Reads `commands/ledger.jsonl` only. |
| `setup_snapshot` | `hermes_runtime/commands/setup_snapshot.py` | Summarizes the installed service, runtime ref, current version, commit, and important directories from Hat admin. | Reads systemd metadata and runtime state files only. It does not read env file contents and does not use sudo. |
| `install_hermes` | `hermes_runtime/commands/install_hermes.py` | Installs upstream Hermes Agent after the Tinyhat runtime is alive, and skips reinstalling when `hermes` already exists. | May install Debian prerequisites as root, then runs `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash`. By default Tinyhat passes `--skip-browser`; set `TINYHAT_HERMES_INSTALL_ARGS` to override. Verifies Hermes messaging and voice dependencies, then preinstalls the Codex auth quick commands plus the matching Hermes menu plugin in `~/.hermes/config.yaml` so a later Telegram connection is fast. The prerequisite set includes `ffmpeg` for Telegram TTS voice bubbles, `ripgrep` for fast search, `build-essential` for native modules, and `xclip`/`wl-clipboard` for Linux desktop image paste. Result fields distinguish no-op from install: `installed_now` means this command ran the installer; `installed_after` means Hermes is present after the command. |
| `hermes_status` | `hermes_runtime/commands/hermes_status.py` | Checks Hermes Agent through its public CLI. | Read-only. Runs `hermes --version`, `hermes status`, and `hermes status --all`, then returns bounded stdout/stderr for Hat admin. |
| `tinyhat_plugin_status` | `hermes_runtime/commands/tinyhat_plugin_status.py` | Shows the Tinyhat plugin version/commit installed in Hermes and the configured target version/commit. | Read-only. Reads the installed plugin manifest and source metadata, then uses a temporary checkout of the public plugin repo to read the target manifest. |
| `check_tinyhat_plugin_update` | `hermes_runtime/commands/check_tinyhat_plugin_update.py` | Checks whether the configured Tinyhat plugin channel has moved beyond the installed plugin. | Read-only. Resolves the configured plugin ref, compares it with `.tinyhat-plugin-source.json`, and reports `update_available`; it does not install, enable, restart, or change Hermes. |
| `install_tinyhat_plugin` | `hermes_runtime/commands/install_tinyhat_plugin.py` | Installs the Tinyhat plugin after Hermes Agent is present, and skips reinstalling when the plugin already exists. | Resolves `TINYHAT_PLUGIN_REF` (default `channels/lts`) from `TINYHAT_PLUGIN_REPO_URL` (default `https://github.com/tinyhat-ai/tinyhat.git`), prepares that checkout, runs `hermes plugins install file://... --enable`, then runs `hermes plugins enable tinyhat`. Records repo/ref/commit in `.tinyhat-plugin-source.json`. Does not configure Telegram or read Tinyhat platform credentials. |
| `update_tinyhat_plugin` | `hermes_runtime/commands/update_tinyhat_plugin.py` | Updates the Tinyhat plugin independently of the Tinyhat runtime. | Resolves the configured plugin ref, compares it with the installed repo/ref/commit metadata, and reinstalls through `hermes plugins install file://... --enable --force` only when the target changed or the plugin is missing. It verifies the installed plugin after the update. A long-running Hermes Telegram gateway may still need a Hermes restart to reload plugin commands. |
| `configure_telegram` | `hermes_runtime/commands/configure_telegram.py` | Configures Hermes Agent to use the Telegram bot assigned to this Computer. | Calls the computer-authenticated Tinyhat setup endpoint while the agent has a short-lived setup grant, writes `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, and `TELEGRAM_HOME_CHANNEL` into Hermes env files, installs the Telegram quick commands documented below, configures local STT plus automatic vision routing, clears Telegram webhook delivery for the bot, and starts `hermes gateway`. The token is not returned in the command result; when the runtime posts a successful command result, the platform marks the Computer/agent active and revokes the setup grant so the token cannot be fetched again. |
| `apply_config` | `hermes_runtime/commands/apply_config.py` | Applies Tinyhat runtime config changes after the settings Mini App saves a secret. | Fetches the latest computer-authenticated runtime secret map, writes the values into Hermes env files, reloads the updated keys into the runtime process, records Tinyhat-managed terminal aliases for every valid saved env name (`_HERMES_FORCE_<ENV_NAME>`), sends the owner a short notice, and restarts `hermes gateway` so Hermes loads added, updated, or removed secret env vars. Command results include only secret names and env-file paths, never secret values. |
| `start_hermes` | `hermes_runtime/commands/start_hermes.py` | Starts Hermes Agent messaging again on an already-configured Computer. | Runs `hermes gateway status` and, only if the gateway is not already healthy, runs `hermes gateway start` with the same foreground fallback used by local/Docker setup. It does not fetch bot tokens, write credentials, change Telegram webhooks, stop Tinyhat runtime, or unassign the Computer. |
| `stop_hermes` | `hermes_runtime/commands/stop_hermes.py` | Stops Hermes Agent messaging before Tinyhat parks or reassigns a Telegram bot. | Runs `hermes gateway stop`, checks gateway status, and terminates the foreground `hermes gateway run` process used by local/Docker fallback mode. It does not stop the Tinyhat runtime service, change Telegram webhooks, remove credentials, or unassign the Computer. |
| `codex_limits` | `hermes_runtime/commands/codex_limits.py` | Shows the OpenAI Codex subscription windows and credits visible to the user's Codex auth on this Computer. | Starts the Codex CLI installed during provisioning with `codex app-server --listen stdio://`, initializes the app-server, calls `account/rateLimits/read`, writes the last structured JSON response to `codex/last_limits.json` under the runtime state directory, and returns a readable summary. It does not read or return OpenAI auth tokens, parse terminal logs, or call the normal OpenAI REST API. |
| `stage_update` | `hermes_runtime/commands/stage_update.py` | Downloads or prepares a target runtime version without changing the running process. | Writes `staged/VERSION`, `staged/metadata.json`, a staged `staged/runtime/hermes_runtime` package, and the import-safe bootstrap when the target release has one. When `target_sha` is present, downloads that immutable commit instead of a movable tag/channel. Does not switch versions until `activate_update`. |
| `activate_update` | `hermes_runtime/commands/activate_update.py` | Requests activation of an already staged update. | Writes `ACTIVATE_ON_RESTART` and exits after reporting success so the process manager restarts the runtime. |
| `restart_runtime_service` | `hermes_runtime/commands/restart_runtime_service.py` | Restarts the Tinyhat runtime service/process so startup can take effect, including an already activated staged update. | Requests process exit after the command result is reported. Requires systemd or Docker restart policy to start the runtime again. Does not reboot the VPS or restart Hermes Agent separately. |

## Telegram Codex auth quick commands

`configure_telegram` also prepares Hermes quick commands in
`~/.hermes/config.yaml`. Hermes quick commands are dispatchable when typed, but
Hermes does not add them to the Telegram command menu by themselves. To keep
Hermes as the only code that calls Telegram `setMyCommands`, Tinyhat also
installs a small user plugin at `~/.hermes/plugins/tinyhat-codex` that
registers the same underscore commands with Hermes' documented plugin command
registry. The same plugin registers `openai-codex-stt`, a Hermes transcription
provider that uses the Codex/OpenAI auth connected by `/codex_auth` and OpenAI's
audio transcription API. Tinyhat does not select that provider automatically
because Codex subscription auth may not include API-billed audio transcription;
new assignments keep local STT active unless the operator opts into the Codex
STT provider. The command bodies still call the runtime helpers below; the
plugin exists so Hermes can include the entries when it builds Telegram
BotCommands. Tinyhat also writes Hermes'
`platforms.telegram.extra.command_menu` priority config so these commands are
near the top while Hermes keeps its default commands.

| Telegram command | What it does |
| --- | --- |
| `/codex_auth` | Starts the official Codex CLI device-code auth flow in the background. The helper sends the authorization link as a Telegram button, sends the device code as a separate copyable message, waits for OpenAI to finish the device flow on this Computer, asks Hermes through its formal model picker to import/switch to OpenAI Codex, registers `openai-codex-stt` settings without making it the active STT provider, sends a short restart notice, and restarts the Telegram gateway so the next reply uses the new model credential while local STT remains the voice fallback. |
| `/codex_auth_status` | Shows whether the helper is still running and checks both Hermes Codex auth and Codex CLI auth status. |
| `/codex_auth_log` | Shows the recent bounded auth log if the device-code output needs to be resent or debugged. |
| `/codex_limits` | Reads the current OpenAI Codex account limits through `codex app-server --listen stdio://` and shows the remaining primary and weekly windows as progress bars, reset times, plan type, credits, and reset-credit count when Codex returns them. |

The Telegram command menu uses underscores because Telegram clients and the Bot
API do not reliably handle hyphenated slash commands. The runtime also installs
`codex-auth` as a best-effort Hermes quick-command alias for typed chat input,
but `/codex_auth` is the reliable command.

These commands are installed as both quick commands and plugin-registered
Hermes commands. The quick commands keep the behavior zero-token and available
when typed; the plugin registrations make the commands visible in `/help`,
autocomplete, and Telegram's menu. They are installed only when Telegram is
connected because the device-code flow needs a private channel where the link
and code can be delivered. The active device code is treated as sensitive: it
is sent to the configured
`TELEGRAM_HOME_CHANNEL`, but it is not returned in the Tinyhat runtime command
result and is not shown in Hat admin. The final OpenAI credentials are written
locally by Codex CLI and Hermes on the Computer; the Tinyhat platform never
receives those tokens. `/codex_limits` uses the Codex CLI auth indirectly
through the Codex app-server process. It returns usage windows and credit
counts, not auth tokens. The command writes the last structured app-server
response to `codex/last_limits.json` under the runtime state directory so
operators can inspect the API result without scraping CLI logs.
The Codex app-server API is currently an experimental Codex CLI surface, so the
runtime treats it as best-effort: if the upstream shape changes, the command
returns a readable unavailable/error payload instead of exposing credentials or
crashing the runtime loop.

## How runtime updates work

The update path keeps discovery, preparation, activation, and restart visible so
a running Computer does not change code while work is in progress.

```text
Operator or schedule
        |
        v
check_update
  - compares current/VERSION and current/COMMIT_SHA with the selected target
  - for LTS/latest, expects the platform to resolve the channel to a final tag
  - writes updates/last_check.json
  - reports the result to the platform
  - does not download, stage, activate, or restart anything
        |
        v
stage_update
  - prepares the exact selected ref
  - uses target_sha as the immutable download ref when the platform supplies it
  - writes staged/VERSION, staged/metadata.json, and staged runtime package code
  - current/VERSION is unchanged, so the running runtime keeps using old code
        |
        v
activate_update
  - writes ACTIVATE_ON_RESTART
  - reports command success to the platform
  - asks only tinyhat-hermes-runtime.service to exit
  - this is normally enough to make the staged update take effect
        |
        |  restart_runtime_service is the optional manual restart lever:
        |  it only asks the same service/process to exit, without changing
        |  staged files or activation state.
        |
        v
systemd restarts tinyhat-hermes-runtime.service
        |
        v
runtime startup promotes staged -> current
  - staged runtime package code replaces the installed hermes_runtime package
  - the small import-safe bootstrap is replaced when the staged release has one
  - current/VERSION now contains the staged ref
  - current/COMMIT_SHA is updated when the staged metadata includes a sha
  - staged files and ACTIVATE_ON_RESTART are cleared
  - the runtime re-executes itself so Python imports the new command whitelist
```

The new runtime version is used after the **tinyhat Hermes runtime service**
restarts and starts up again. A VPS reboot is not required. The activation
command does not restart the Hermes framework separately; it restarts the small
Tinyhat runtime process that sends heartbeats and executes the whitelisted
commands in this repository.

The launcher runs `tinyhat_hermes_runtime_bootstrap.py`, a tiny import-safe
bootstrap outside the `hermes_runtime` package. If a process is interrupted
between moving `hermes_runtime` to `.previous` and moving `.next` into place,
the next start repairs that package directory before importing runtime code.
If activation still fails, the process keeps heartbeating and records
`updates/last_activation_error.json`; `update_status` and heartbeat metrics can
surface that error from Hat admin.

`update_status` is the read-only command to run before or after any step. It
shows the currently installed runtime ref, any staged ref waiting for
activation, and the latest update-check result. If the runtime has changed since
the last update check was computed, that cached result is marked stale and its
`update_available` value is cleared so operators know to run `check_update`
again. `restart_runtime_service` is the manual service restart command when you
need the runtime process to start again without staging or activating anything
new.

`running_version` is the direct post-update proof command. It returns the
`hermes_runtime.__version__` value and the file path from the package imported
by the Python process that handled the command. That makes it useful when state
files or platform metadata disagree with what the service is actually running.

Update checks use the same version rule everywhere:

- `custom` can point at any explicit tag or commit selected by an operator.
- `lts` and `latest` only report `update_available=true` when the target is a
  concrete final release tag (`vX.Y.Z`) newer than the installed final version.
- A raw channel selector such as `channels/lts` is installable, but it is not a
  version decision by itself. Protected channel branches can point at merge
  commits that contain the release tag, so the platform should resolve the
  channel to the final tag first and send that tag to `check_update`.

The daily scheduled update check runs only the discovery part (`check_update`).
It reports that an update is available, but it does not stage or activate a new
version by itself.

Without `TINYHAT_RUNTIME_UPDATE_SOURCE_DIR`, staging downloads runtime source
archives from `codeload.github.com`. Production Computers therefore need
ordinary outbound HTTPS access to GitHub's archive host. Local development can
set `TINYHAT_RUNTIME_UPDATE_SOURCE_DIR` when the goal is to test unmerged local
runtime code instead of a published tag or commit.

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

Channel branches are installer selectors, not update-decision values. The
platform resolves `channels/latest` or `channels/lts` to the concrete final tag
they contain before asking a Computer whether an update is available. This keeps
protected channel-branch merge commits from looking like newer runtime versions
when the Computer is already running that final tag.

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

The installer expects Git plus `curl` and `xz-utils` on Linux; the desktop app
also expects `g++` or `build-essential` for native modules. Tinyhat installs the
recommended apt package set during Computer provisioning, before assignment:
`ca-certificates`, `curl`, `git`, `xz-utils`, `build-essential`, `ffmpeg`,
`ripgrep`, `xclip`, and `wl-clipboard`. `ffmpeg` keeps Hermes TTS replies as
Telegram voice bubbles for providers that do not emit Opus directly, and
`xclip`/`wl-clipboard` cover the Linux CLI image-paste path. The upstream
installer still owns the Hermes clone, Python 3.11 via uv, Node.js 22, a virtual
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

This foundation installs Hermes Agent after the Tinyhat runtime starts, but it
does not assign a Computer to an agent yet. It proves the management loop first:
heartbeat, attestation, command dispatch, Hermes install/status checks, and
restart-activated updates.

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
