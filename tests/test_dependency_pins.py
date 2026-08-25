#!/usr/bin/env python3
"""
The installed dependency set must be exactly the one `requirements.txt` pins.

THIS TEST IS SUPPOSED TO FAIL ON A MISMATCHED ENVIRONMENT -- including a
developer's global site-packages. That is the entire point of the file, not a
bug in it. The deployed image runs `pip install -r requirements.txt` inside
`python:3.12-slim`, so a test run against any other version set proves nothing
about production. Do NOT "fix" a failure here by loosening the assertion or by
editing the pin; build a venv and install the pinned set:

    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt     # Windows
    .venv/bin/python -m pip install -r requirements.txt         # Linux/macOS

CI runs this file FIRST so a version mismatch fails with an unmistakable
message instead of surfacing as a bizarre API error four steps later.

Run directly: `python tests/test_dependency_pins.py`
"""

import os
import sys
from importlib import metadata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(REPO_ROOT, "requirements.txt")

# Dockerfile: FROM python:3.12-slim
EXPECTED_PYTHON = (3, 12)


def _requirement_lines():
    """Non-blank, non-comment lines of requirements.txt, stripped."""
    with open(REQUIREMENTS, encoding="utf-8") as fh:
        raw = fh.read()
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def parse_pins():
    """-> ({distribution: version}, [lines that were not exact `==` pins])."""
    pins, loose = {}, []
    for line in _requirement_lines():
        # Strip an inline comment, then any environment marker.
        line = line.split("#", 1)[0].strip()
        line = line.split(";", 1)[0].strip()
        if not line:
            continue
        if line.count("==") == 1:
            name, version = line.split("==")
            name, version = name.strip(), version.strip()
            if name and version:
                pins[name] = version
                continue
        loose.append(line)
    return pins, loose


def test_requirements_file_exists_and_is_not_empty():
    assert os.path.isfile(REQUIREMENTS), f"requirements.txt not found at {REQUIREMENTS}"
    assert _requirement_lines(), "requirements.txt has no requirement lines"


def test_every_requirement_is_an_exact_pin():
    """A range or an unpinned name means CI, the image and the developer can all
    resolve different versions -- which is exactly the drift this file exists to
    stop."""
    _, loose = parse_pins()
    assert not loose, (
        "requirements.txt must pin every dependency with an exact `==` version so "
        "the image, CI and local runs install byte-identical packages. These lines "
        f"are not exact pins: {loose}"
    )


def test_installed_versions_match_the_pins():
    pins, _ = parse_pins()
    assert pins, "no exact pins were parsed out of requirements.txt"

    mismatches = []
    for name, pinned in sorted(pins.items()):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            mismatches.append(
                f"`{name}` is pinned to {pinned} in requirements.txt but is NOT INSTALLED "
                f"in this environment ({sys.executable})."
            )
            continue
        if installed != pinned:
            mismatches.append(
                f"`{name}` {installed} is installed but requirements.txt pins {pinned}. "
                f"The deployed image installs the pinned version; a test run against "
                f"anything else proves nothing about production."
            )

    assert not mismatches, (
        "\n\nDEPENDENCY MISMATCH -- this environment is not the one that ships:\n  "
        + "\n  ".join(mismatches)
        + "\n\nRun `pip install -r requirements.txt`, ideally in a venv, and re-run.\n"
        f"Interpreter: {sys.executable}\n"
    )


def test_python_version_matches_the_image():
    got = sys.version_info[:2]
    assert got == EXPECTED_PYTHON, (
        f"Python {got[0]}.{got[1]} is running, but Dockerfile is "
        f"`FROM python:{EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}-slim`. Behaviour that "
        "depends on the interpreter version (datetime deprecations, asyncio internals) "
        "will not be reproduced here. Use Python "
        f"{EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}."
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(tests) >= 4, (
        "expected at least 4 tests, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(tests), ", ".join(t.__name__ for t in tests) or "none")
    )
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} dependency-pin tests passed!")
