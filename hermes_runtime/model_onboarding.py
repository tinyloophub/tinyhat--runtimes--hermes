"""Seed an unconfigured Hermes model before a first chat channel starts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from hermes_runtime.openrouter_stt import hermes_python
from hermes_runtime.plugin_manager import hermes_home


def has_selection(config: dict) -> bool:
    current = config.get("model")
    selected = (
        current.get("default") or current.get("model")
        if isinstance(current, dict) else current
    )
    provider = current.get("provider") if isinstance(current, dict) else None
    return bool(selected or provider not in (None, "", "auto", "openrouter"))


def _configure_file():
    # Public Hermes YAML is read by its own interpreter, never by the supervisor.
    import yaml
    from hermes_runtime.agent_systems import _write_atomic
    from hermes_runtime.commands.configure_telegram import _upsert_env_file

    payload = json.load(sys.stdin)
    home = Path(payload["home"])
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = config or {}
    if not isinstance(config, dict):
        raise ValueError("Invalid Hermes configuration")
    if has_selection(config):
        print(json.dumps({"changed": False}))
        return
    setup = payload.get("setup") or {}
    key = setup.get("openrouter_api_key")
    model = setup.get("openrouter_default_model")
    if not key or not model:
        raise ValueError("Initial model access is not ready")
    # Only the new Computer's empty model is initialized. Subscription/custom
    # providers, selected models, owner files and other channels are preserved.
    _upsert_env_file(home / ".env", {"OPENROUTER_API_KEY": key})
    config["model"] = {"default": model, "provider": "openrouter", "max_tokens": 4096}
    _write_atomic(path, yaml.safe_dump(config, sort_keys=False), 0o600)
    print(json.dumps({"changed": True}))


def _configure(setup):
    result = subprocess.run(
        [str(hermes_python()), "-c",
         "from hermes_runtime.model_onboarding import _configure_file; _configure_file()"],
        input=json.dumps({"home": str(hermes_home()), "setup": setup}),
        text=True, capture_output=True, timeout=30,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (
            str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH"),
        )))},
    )
    if result.returncode:
        # Child output may contain YAML or secrets. Never forward it.
        raise RuntimeError("Hermes model setup is not ready. Retry channel setup.")
    return json.loads(result.stdout)["changed"] is True


async def ensure_model(setup):
    return await asyncio.to_thread(_configure, setup)
