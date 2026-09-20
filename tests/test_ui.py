"""Runs the jsdom UI suites, when a Node toolchain is available."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_DIR = Path(__file__).parent / "ui"
SUITES = sorted(UI_DIR.glob("*.test.mjs"))


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; run `npm install` in tests/ui to enable the DOM tests")
    return node


def _require_jsdom(node: str) -> None:
    probe = subprocess.run(
        [node, "--input-type=module", "-e", "import('jsdom')"],
        cwd=UI_DIR,
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"jsdom is not installed; run `npm install` in {UI_DIR}")


@pytest.mark.parametrize("suite", SUITES, ids=lambda s: s.name)
def test_dom_behaviour(suite: Path):
    node = _node()
    _require_jsdom(node)
    result = subprocess.run([node, str(suite)], cwd=UI_DIR, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ui_suite_declares_its_dependency():
    manifest = json.loads((UI_DIR / "package.json").read_text())
    assert "jsdom" in manifest["dependencies"]
