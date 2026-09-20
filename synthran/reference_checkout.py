"""Materialize the pinned 5g-Ansible execution reference for delegated roles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json"
CACHE_ROOT = ROOT / ".synthran/reference/sopnode-5g-ansible"


def _git(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise SystemExit("git is required to materialize the pinned 5g-Ansible reference") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        raise SystemExit(
            f"Failed to materialize the pinned 5g-Ansible reference: {detail or error}"
        ) from error
    return (result.stdout or "").strip()


def _contract() -> tuple[str, str]:
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    repository = str(data["repository"])
    commit = str(data["commit"])
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit.lower()):
        raise SystemExit(f"Invalid pinned 5g-Ansible commit in {CONTRACT}: {commit}")
    return repository, commit


def _verify(path: Path, commit: str) -> None:
    if not (path / ".git").exists():
        raise SystemExit(f"Pinned 5g-Ansible cache is not a Git checkout: {path}")
    actual = _git("rev-parse", "HEAD", cwd=path, capture=True)
    if actual != commit:
        raise SystemExit(
            f"Pinned 5g-Ansible cache drifted: expected {commit}, found {actual}"
        )
    status = _git("status", "--porcelain", cwd=path, capture=True)
    if status:
        raise SystemExit(f"Pinned 5g-Ansible cache has local modifications: {path}")
    required = path / "roles/setup/containerd/tasks/main.yml"
    if not required.is_file():
        raise SystemExit(f"Pinned 5g-Ansible cache is incomplete: missing {required}")


def ensure_execution_reference() -> Path:
    """Return an exact, immutable checkout of the #51 execution reference."""

    repository, commit = _contract()
    target = CACHE_ROOT / commit
    if target.exists():
        _verify(target, commit)
        return target

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{commit[:12]}-", dir=str(CACHE_ROOT)))
    try:
        _git("init", "-q", str(temporary))
        _git("-C", str(temporary), "remote", "add", "origin", repository)
        _git("-C", str(temporary), "fetch", "--depth", "1", "origin", commit)
        _git("-C", str(temporary), "checkout", "--detach", "-q", commit)
        _verify(temporary, commit)
        try:
            os.replace(temporary, target)
        except OSError:
            if not target.exists():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
            _verify(target, commit)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


if __name__ == "__main__":
    print(ensure_execution_reference())
