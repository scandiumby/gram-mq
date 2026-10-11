"""Single-container launcher for PaaS deploys: API + worker.

Runs the two standard entry points (`python -m gram_mq.api`,
`python -m gram_mq.worker`) as subprocesses and stops both when either
one exits or the container receives SIGTERM/SIGINT — one dead service
must not leave a half-alive container (the platform restarts it whole).
A deployment artifact in the spirit of the systemd units next to it,
not application code: both assembly points stay untouched.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

SERVICES = ("gram_mq.api", "gram_mq.worker")


def _shutdown(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()


def main() -> int:
    procs = [subprocess.Popen([sys.executable, "-m", name]) for name in SERVICES]

    def on_signal(signum: int, frame: object) -> None:
        _shutdown(procs)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    failure = 0
    while procs:
        pid, status = os.wait()
        procs = [proc for proc in procs if proc.pid != pid]
        code = os.waitstatus_to_exitcode(status)
        if failure == 0 and code not in (0, -signal.SIGTERM, -signal.SIGINT):
            failure = code or 1
        if procs:
            _shutdown(procs)  # the survivor must not outlive its sibling
    return failure


if __name__ == "__main__":
    raise SystemExit(main())
