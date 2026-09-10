# Computer timing files

`hermes_runtime/computer_metadata.py` projects the optional, authenticated
`computer_metadata` heartbeat response into `~/tinyhat/computer.json` and a
short README. It is host metadata persistence, with no upstream framework or
product-policy behavior. The platform owns the measurements.

Run `python -m unittest tests.test_computer_metadata -v`. These tests cover the
created → warm assignment → ready snapshots, safe field filtering, missing
historical data, restart/idempotency, atomic replacement failure and command
continuation after a metadata write failure.

For the platform/runtime contract smoke, provide a directory containing actual
local-test heartbeat responses named `created.json`, `assigned.json`, and
`ready.json` to `python scripts/smoke_computer_metadata.py <directory>`. The
script uses a temporary home, feeds those recorded responses to the real runtime
heartbeat handler, and checks the resulting files. It makes no cloud or billing
calls. Run it inside Linux with the runtime checkout and response directory
mounted read-only. Deployed verification remains separate: open the Computer's
terminal, run `cat ~/tinyhat/computer.json`, and compare both timings with the
platform record.
