"""Installing the package must not drag the test and lint tooling along.

`pyproject.toml` is the single source of truth for both dependency sets; the
only thing written down twice is the runtime set, which `requirements.txt`
repeats for installs that do not go through the package. Nothing enforces
either property at install time, so it is enforced here.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DEV_ONLY = {"pytest", "pytest-asyncio", "ruff", "httpx"}
"""Never a runtime dependency. `httpx` is the easy mistake: the inspector talks
through `httpx2`, which `mcp` brings in, and only the tests import `httpx`."""


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def requirements(name: str) -> set[str]:
    """The pinned lines of a requirements file, without comments or includes."""
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith(("#", "-r ", "-e ", "--"))
    }


def name_of(requirement: str) -> str:
    """The bare package name out of a requirement line."""
    return requirement.split(";")[0].split(">")[0].split("=")[0].split("[")[0].strip()


# --------------------------------------------------- what an install pulls in


def test_the_dev_tooling_is_an_extra_not_a_dependency():
    """`pip install pymcpinspector` must bring the app and nothing else."""
    runtime = {name_of(d) for d in project()["dependencies"]}
    assert not runtime & DEV_ONLY


def test_the_dev_extra_holds_the_tooling():
    dev = {name_of(d) for d in project()["optional-dependencies"]["dev"]}
    assert dev == DEV_ONLY


def test_the_built_metadata_marks_the_tooling_as_opt_in():
    """The property as pip actually sees it, not as the toml describes it.

    Every dev requirement has to carry an `extra == "dev"` marker; without one
    it would be installed unconditionally however the toml is laid out.
    """
    try:
        meta = distribution("pymcpinspector").metadata
    except PackageNotFoundError:
        pytest.skip("pymcpinspector is not installed; run `pip install -e \".[dev]\"`")

    required = meta.get_all("Requires-Dist") or []
    unconditional = {name_of(line) for line in required if "extra ==" not in line}
    assert not unconditional & DEV_ONLY, f"installed unconditionally: {unconditional & DEV_ONLY}"
    assert "dev" in (meta.get_all("Provides-Extra") or [])


# ------------------------------------------------------- the one repeated set


def test_the_runtime_set_is_repeated_faithfully():
    """requirements.txt exists for installs that skip the package metadata."""
    assert set(project()["dependencies"]) == requirements("requirements.txt")


def test_there_is_no_second_requirements_file_to_drift():
    """The dev set lives only in pyproject, so it cannot disagree with itself."""
    assert not (ROOT / "requirements-dev.txt").exists()


def test_the_package_version_matches_the_module():
    from pymcpinspector import __version__

    assert project()["version"] == __version__
