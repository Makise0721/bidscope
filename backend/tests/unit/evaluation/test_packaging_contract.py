"""RED-phase packaging contracts for Task 18 evaluation release blockers."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_APPROVED_EVALUATION_RESOURCES = {
    "bidscope/evaluation/corpus/synthetic-notices-v1.jsonl": (
        _REPOSITORY_ROOT / "eval/corpus/synthetic-notices-v1.jsonl"
    ),
    "bidscope/evaluation/data/claims-v1.jsonl": _REPOSITORY_ROOT / "eval/data/claims-v1.jsonl",
    "bidscope/evaluation/data/dedup-v1.jsonl": _REPOSITORY_ROOT / "eval/data/dedup-v1.jsonl",
    "bidscope/evaluation/data/e2e-v1.jsonl": _REPOSITORY_ROOT / "eval/data/e2e-v1.jsonl",
    "bidscope/evaluation/data/intent-v1.jsonl": _REPOSITORY_ROOT / "eval/data/intent-v1.jsonl",
    "bidscope/evaluation/data/retrieval-v1.jsonl": (
        _REPOSITORY_ROOT / "eval/data/retrieval-v1.jsonl"
    ),
}


def _build_wheel(output_dir: Path) -> Path:
    uv = shutil.which("uv")
    if uv is not None:
        command = [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(output_dir),
        ]
    elif importlib.util.find_spec("build") is not None:
        command = [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
        ]
    else:
        pytest.fail(
            "Product behavior: BidScope must provide uv or python -m build so its "
            "approved evaluation resources can be verified in the distributable package."
        )

    completed = subprocess.run(
        command,
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stdout + "\n" + completed.stderr).strip()
        pytest.fail(
            "Product behavior: `uv build --wheel` (or `python -m build --wheel`) "
            "must produce an inspectable BidScope wheel; command failed with:\n"
            f"{details}"
        )

    wheels = sorted(output_dir.glob("*.whl"))
    if not wheels:
        pytest.fail(
            "Product behavior: the BidScope wheel build must emit a .whl artifact "
            "for installation and evaluation-resource inspection."
        )
    return wheels[-1]


def test_wheel_contains_approved_evaluation_resources_under_bidscope(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "wheelhouse")

    with zipfile.ZipFile(wheel) as archive:
        packaged_paths = set(archive.namelist())

    assert "bidscope/cli.py" in packaged_paths
    missing = sorted(set(_APPROVED_EVALUATION_RESOURCES) - packaged_paths)
    assert not missing, (
        "Product behavior: the installed bidscope package must include every approved "
        f"evaluation JSONL resource; missing={missing}"
    )
    with zipfile.ZipFile(wheel) as archive:
        for packaged_path, source_path in _APPROVED_EVALUATION_RESOURCES.items():
            assert archive.read(packaged_path) == source_path.read_bytes()
