"""Create/reuse SynthRAN's shared repository runtime and install optional extras."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from .reference_checkout import ensure_execution_reference

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"


def shared_python() -> Path:
    """Return the single repository-local Python runtime, creating it if needed."""

    if VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK):
        return VENV_PYTHON
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    if not VENV_PYTHON.is_file() or not os.access(VENV_PYTHON, os.X_OK):
        raise SystemExit(f"Shared SynthRAN virtual environment is incomplete: {VENV_PYTHON}")
    return VENV_PYTHON


def ensure(extra: str, log: Path) -> None:
    declaration = (ROOT / "pyproject.toml").read_bytes()
    venv_config = Path(sys.prefix) / "pyvenv.cfg"
    generation = venv_config.stat().st_mtime_ns if venv_config.exists() else 0
    identity = f"{sys.executable}:{sys.version}:{generation}:{extra}".encode()
    stamp = (
        ROOT / ".synthran/runtime" / hashlib.sha256(identity + declaration).hexdigest()
    )
    if stamp.exists():
        return
    stamp.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-e",
                f"{ROOT}[{extra}]",
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    if result.returncode:
        raise SystemExit(f"Runtime installation failed; see {log}")
    stamp.touch()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    if args.action == "python":
        print(shared_python())
        return
    if args.log is None:
        parser.error("--log is required when preparing a runtime extra")
    ensure(args.action, args.log)
    if args.action == "deployment":
        ensure_execution_reference()


if __name__ == "__main__":
    main()
