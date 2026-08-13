"""Focused tests for the local Hermes onboarding greeting command."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hermes_runtime.commands import COMMAND_MODULES, onboarding_greeting


class OnboardingGreetingCommandTests(unittest.TestCase):
    def test_generates_with_skill_then_delivers_locally(self) -> None:
        run_process = AsyncMock(
            side_effect=[
                {"ok": True, "stdout": "Hi, I'm ready to work with you."},
                {"ok": True, "stdout": '{"ok":true}'},
            ]
        )
        with (
            patch.object(
                onboarding_greeting,
                "find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch.object(onboarding_greeting, "run_process", run_process),
        ):
            result = asyncio.run(
                onboarding_greeting.run(
                    None,
                    {
                        "spec": {
                            "agent_name": "The Forecaster",
                            "agent_description": "Turns instinct into a plan.",
                        }
                    },
                )
            )

        self.assertTrue(result["greeting_sent"])
        self.assertEqual(result["delivery_target"], "telegram")
        generation_args = run_process.await_args_list[0].args[0]
        self.assertIn("--oneshot", generation_args)
        self.assertEqual(
            generation_args[generation_args.index("--skills") + 1],
            "tinyhat:tinyhat-onboarding-greeting",
        )
        prompt = generation_args[generation_args.index("--oneshot") + 1]
        self.assertIn('"display_name": "The Forecaster"', prompt)
        self.assertIn("Do not call yourself Hermes", prompt)
        self.assertEqual(
            run_process.await_args_list[0].kwargs["env"],
            {"TINYHAT_ONBOARDING_GREETING_TURN": "1"},
        )
        delivery_args = run_process.await_args_list[1].args[0]
        self.assertEqual(delivery_args[1:5], ["send", "--to", "telegram", "--json"])
        self.assertEqual(delivery_args[-1], "Hi, I'm ready to work with you.")

    def test_does_not_deliver_empty_or_oversized_model_output(self) -> None:
        for output in ("", "x" * (onboarding_greeting.MAX_GREETING_CHARS + 1)):
            run_process = AsyncMock(return_value={"ok": True, "stdout": output})
            with (
                patch.object(
                    onboarding_greeting,
                    "find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch.object(onboarding_greeting, "run_process", run_process),
                self.assertRaisesRegex(RuntimeError, "invalid onboarding"),
            ):
                asyncio.run(onboarding_greeting.run(None, {}))
            self.assertEqual(run_process.await_count, 1)

    def test_generation_or_delivery_failure_is_not_reported_as_sent(self) -> None:
        cases = [
            [{"ok": False, "stdout": ""}],
            [
                {"ok": True, "stdout": "Hello."},
                {"ok": False, "stdout": ""},
            ],
        ]
        for results in cases:
            run_process = AsyncMock(side_effect=results)
            with (
                patch.object(
                    onboarding_greeting,
                    "find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch.object(onboarding_greeting, "run_process", run_process),
                self.assertRaisesRegex(RuntimeError, "could not"),
            ):
                asyncio.run(onboarding_greeting.run(None, {}))

    def test_command_is_registered(self) -> None:
        self.assertEqual(
            COMMAND_MODULES["onboarding_greeting"],
            "hermes_runtime.commands.onboarding_greeting",
        )


if __name__ == "__main__":
    unittest.main()
