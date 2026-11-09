"""The dispatcher and the server share contracts, never each other's runtime."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code: str) -> None:
    # A fresh interpreter is required: pytest itself has already imported the server.
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {SRC!r}); {code}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_contracts_import_neither_side() -> None:
    _run(
        "import cairn.contracts.models;"
        "leaked = [m for m in sys.modules if m.startswith(('cairn.server', 'cairn.dispatcher'))];"
        "assert not leaked, leaked"
    )


def test_dispatcher_client_does_not_import_the_server() -> None:
    _run(
        "import cairn.dispatcher.protocol.client;"
        "leaked = [m for m in sys.modules if m.startswith('cairn.server')];"
        "assert not leaked, leaked"
    )
