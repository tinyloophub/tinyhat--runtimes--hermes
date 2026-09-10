"""On-disk Computer timing metadata and heartbeat failure isolation."""
import asyncio
import copy
import json
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from hermes_runtime import computer_metadata as metadata
from hermes_runtime.main import RuntimeContext, _heartbeat_once

CREATED = {
    "schema": metadata.SCHEMA, "handle": "tinyhat/computers/example",
    "created_at": "2026-09-10T10:00:00+00:00",
    "creation": {"started_at": "2026-09-10T10:00:00+00:00", "ready_at": "2026-09-10T10:02:00+00:00", "duration_ms": 120000},
    "assignment": None,
}
ASSIGNED = {
    "computer_id": "cmp_" + "c" * 32, "agent_id": "agt_" + "a" * 22,
    "system": "codex", "source": "warm",
    "started_at": "2026-09-10T11:00:00+00:00", "assigned_at": "2026-09-10T11:00:00.250000+00:00",
    "ready_at": None, "allocation_duration_ms": 250, "duration_ms": None,
}


class ComputerMetadataTests(TestCase):
    def test_created_assigned_ready_repeated_and_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            metadata.apply(CREATED, home=home)
            path = home / "tinyhat/computer.json"
            self.assertIsNone(json.loads(path.read_text())["assignment"])
            self.assertTrue((home / "tinyhat/README.md").exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            allocated = {**CREATED, "assignment": ASSIGNED}
            metadata.apply(allocated, home=home)
            self.assertIsNone(json.loads(path.read_text())["assignment"]["duration_ms"])
            ready = {**allocated, "assignment": {**ASSIGNED, "ready_at": "2026-09-10T11:00:01+00:00", "duration_ms": 1000}}
            metadata.apply(ready, home=home)
            before = path.stat().st_mtime_ns
            # No in-memory cache: this also exercises runtime restart behavior.
            with patch.object(metadata.os, "replace", side_effect=AssertionError("unchanged file rewritten")):
                metadata.apply(copy.deepcopy(ready), home=home)
                metadata.apply(None, home=home)
            saved = json.loads(path.read_text())
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(saved["creation"]["duration_ms"], 120000)
            self.assertEqual(saved["assignment"]["duration_ms"], 1000)
            self.assertEqual(list((home / "tinyhat").glob(".metadata-*")), [])

    def test_unknown_fields_are_not_saved_and_invalid_data_preserves_last_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            metadata.apply({**CREATED, "token": "do-not-save", "creation": {**CREATED["creation"], "raw_output": "do-not-save"}}, home=home)
            path = home / "tinyhat/computer.json"
            original = path.read_text()
            self.assertNotIn("do-not-save", original)
            for changed in ({"schema": "unknown"}, {"handle": "../../other"}, {"creation": {"duration_ms": -1}}, {"assignment": []}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    metadata.apply({**CREATED, **changed}, home=home)
                self.assertEqual(path.read_text(), original)

    def test_failed_atomic_replace_preserves_snapshot_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            metadata.apply(CREATED, home=home)
            path = home / "tinyhat/computer.json"
            original = path.read_text()
            with patch.object(metadata.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                metadata.apply({**CREATED, "assignment": ASSIGNED}, home=home)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list((home / "tinyhat").glob(".metadata-*")), [])
            metadata.apply({**CREATED, "assignment": ASSIGNED}, home=home)
            self.assertEqual(json.loads(path.read_text())["assignment"]["source"], "warm")

    def test_symlink_does_not_redirect_metadata_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / "project"
            target.mkdir()
            (home / "tinyhat").symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                metadata.apply(CREATED, home=home)
            self.assertEqual(list(target.iterdir()), [])

    def test_metadata_failure_does_not_stop_heartbeat_command(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = SimpleNamespace(post_json=AsyncMock(return_value={
                "state": "ready", "computer_metadata": CREATED,
                "command": {"type": "runtime_command", "command": {"id": "cmd-example", "kind": "ping"}},
            }))
            ctx = RuntimeContext(platform=platform, state_dir=Path(directory), started_at=0)
            with patch.object(metadata, "apply", side_effect=OSError("disk unavailable")), patch("hermes_runtime.main._maybe_start_command") as command, patch("hermes_runtime.main._refresh_gateway_state", new_callable=AsyncMock), patch("hermes_runtime.main._maybe_start_scheduled_update_check"), patch("hermes_runtime.main._maybe_start_gateway_reconcile"):
                asyncio.run(_heartbeat_once(ctx))
            command.assert_called_once_with(ctx, {"id": "cmd-example", "kind": "ping"})
