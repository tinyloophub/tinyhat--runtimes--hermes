#!/usr/bin/env python3
"""Import-safe launcher for the Tinyhat Hermes runtime.

This file intentionally lives outside the ``hermes_runtime`` package. The
runtime update flow swaps that package directory on restart; if the process is
interrupted mid-swap, this bootstrap can repair ``hermes_runtime.next`` or
``hermes_runtime.previous`` before Python tries to import the package.
"""

from __future__ import annotations

import os
import runpy
import shutil
import sys
from pathlib import Path


PACKAGE_NAME = "hermes_runtime"


def install_prefix() -> Path:
    configured = (os.getenv("TINYHAT_RUNTIME_PREFIX") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent


def recover_interrupted_package_swap(prefix: Path) -> None:
    target_package = prefix / PACKAGE_NAME
    next_package = prefix / f"{PACKAGE_NAME}.next"
    previous_package = prefix / f"{PACKAGE_NAME}.previous"

    if target_package.exists():
        shutil.rmtree(next_package, ignore_errors=True)
        shutil.rmtree(previous_package, ignore_errors=True)
        return

    if next_package.exists():
        next_package.rename(target_package)
        shutil.rmtree(previous_package, ignore_errors=True)
        return

    if previous_package.exists():
        previous_package.rename(target_package)


def main() -> None:
    prefix = install_prefix()
    recover_interrupted_package_swap(prefix)
    prefix_text = str(prefix)
    if prefix_text not in sys.path:
        sys.path.insert(0, prefix_text)
    runpy.run_module("hermes_runtime.main", run_name="__main__")


if __name__ == "__main__":
    main()
