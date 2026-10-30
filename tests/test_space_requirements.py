"""The Spaces install their requirements fresh on every factory reboot, with no
lockfile — a floor-only pin pulls the next major the day it ships (the mcp 2.0
boot crash). Every dependency on a Space's boot path must carry a major cap."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_PATH = {
    "requirements-mcp.txt": {
        "mcp",
        "starlette",
        "uvicorn",
        "matplotlib",
        "huggingface_hub",
        "sentry-sdk",
    },
    "requirements-web.txt": {"gradio", "httpx", "matplotlib", "huggingface_hub", "sentry-sdk"},
}


@pytest.mark.parametrize("name", sorted(BOOT_PATH))
def test_boot_path_dependencies_are_major_capped(name):
    specs = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pkg = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0]
            specs[pkg] = line
    for pkg in BOOT_PATH[name]:
        assert pkg in specs, f"{pkg} missing from {name}"
        assert "<" in specs[pkg], f"{name}: {specs[pkg]!r} has no upper bound"
