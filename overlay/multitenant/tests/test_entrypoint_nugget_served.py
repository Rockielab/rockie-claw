"""Tests for the nugget_served runtime wiring in ``entrypoint.sh``.

The served runtime spawns the same nugget (Goose) binary as the BYOK path,
but against a provider endpoint whose every coordinate comes from deploy-time
environment. ``wire_nugget_served_env()`` (plus its ``wire_served_identity``
and ``wire_served_scrub`` helpers) maps that env onto Goose's provider env and
wires a generic identity instruction + output-scrub list. This repo is PUBLIC,
so the mechanism must be entirely parameterized: it ships NO endpoint, model,
provider, identity, or scrub-term value of its own.

Like the sibling entrypoint tests, we do NOT build the image — we extract the
function bodies and run them in a bash subprocess against PLACEHOLDER env.

Run from the repo root with:

    uv run --with pytest pytest \
      overlay/multitenant/tests/test_entrypoint_nugget_served.py -v
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO_ROOT / "overlay" / "multitenant" / "entrypoint.sh"

# Placeholder values ONLY — never a real served endpoint/key/identity.
PLACEHOLDER_ENV = {
    "SERVED_MODEL_PROVIDER": "openai",
    "SERVED_MODEL_BASE_URL": "https://example.com",
    "SERVED_MODEL_API_KEY": "sk-test",
    "SERVED_MODEL_MODEL_ID": "test-model",
    "SERVED_MODEL_IDENTITY": "Test Identity",
    "SERVED_MODEL_SCRUB": "foo,bar",
}


def _extract_function(name: str) -> str:
    src = ENTRYPOINT.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^{re.escape(name)}\(\)\s*\{{\n(?P<body>.*?)^\}}\n",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(src)
    assert m is not None, f"{name}() not found in entrypoint.sh"
    return m.group("body")


def _harness(extra_setup: str = "") -> str:
    funcs = "".join(
        f"{name}() {{\n{_extract_function(name)}\n}}\n"
        for name in (
            "wire_served_identity",
            "wire_served_scrub",
            "wire_nugget_served_env",
        )
    )
    return (
        'log() { printf "[entrypoint] %s\\n" "$*" >&2; }\n'
        f"{funcs}"
        f"{extra_setup}"
    )


def _run(tmp_path: Path, env: dict[str, str], call: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    full_env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **env}
    return subprocess.run(
        ["bash", "-c", _harness(call)],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


def test_served_env_maps_to_goose_provider_env(tmp_path: Path) -> None:
    """SERVED_MODEL_* placeholders map onto the Goose provider env."""
    call = (
        "wire_nugget_served_env || exit $?\n"
        'printf "GOOSE_PROVIDER=%s\\n" "$GOOSE_PROVIDER"\n'
        'printf "GOOSE_MODEL=%s\\n" "$GOOSE_MODEL"\n'
        'printf "OPENAI_BASE_URL=%s\\n" "$OPENAI_BASE_URL"\n'
        'printf "OPENAI_API_KEY=%s\\n" "$OPENAI_API_KEY"\n'
    )
    proc = _run(tmp_path, PLACEHOLDER_ENV, call)
    assert proc.returncode == 0, proc.stderr
    assert "GOOSE_PROVIDER=openai" in proc.stdout
    assert "GOOSE_MODEL=test-model" in proc.stdout
    assert "OPENAI_BASE_URL=https://example.com" in proc.stdout
    assert "OPENAI_API_KEY=sk-test" in proc.stdout


def test_served_identity_injected_from_env(tmp_path: Path) -> None:
    """The identity instruction is written from SERVED_MODEL_IDENTITY only."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **PLACEHOLDER_ENV}
    proc = subprocess.run(
        ["bash", "-c", _harness("wire_nugget_served_env || exit $?\n")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    hints = (home / ".config" / "goose" / ".goosehints").read_text(encoding="utf-8")
    assert "You are Test Identity" in hints
    assert "served-identity (managed)" in hints
    # Never self-identifies as the underlying model.
    assert "underlying model" in hints


def test_served_identity_idempotent(tmp_path: Path) -> None:
    """Re-running never duplicates the managed identity block."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **PLACEHOLDER_ENV}
    proc = subprocess.run(
        [
            "bash",
            "-c",
            _harness("wire_nugget_served_env >/dev/null 2>&1\nwire_nugget_served_env >/dev/null 2>&1\n"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    hints = (home / ".config" / "goose" / ".goosehints").read_text(encoding="utf-8")
    assert hints.count("served-identity (managed) >>>") == 1


def test_served_scrub_terms_from_env(tmp_path: Path) -> None:
    """SERVED_MODEL_SCRUB is normalized to one term per line in the scrub file."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **PLACEHOLDER_ENV}
    proc = subprocess.run(
        ["bash", "-c", _harness("wire_nugget_served_env || exit $?\n")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    scrub = (home / ".config" / "goose" / ".served-scrub").read_text(encoding="utf-8")
    assert scrub.splitlines() == ["foo", "bar"]


def test_served_scrub_empty_default_is_noop(tmp_path: Path) -> None:
    """With SERVED_MODEL_SCRUB unset the scrub file is empty (a no-op)."""
    env = {k: v for k, v in PLACEHOLDER_ENV.items() if k != "SERVED_MODEL_SCRUB"}
    home = tmp_path / "home"
    home.mkdir()
    full = {"PATH": "/usr/bin:/bin", "HOME": str(home), **env}
    proc = subprocess.run(
        ["bash", "-c", _harness("wire_nugget_served_env || exit $?\n")],
        capture_output=True,
        text=True,
        env=full,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    scrub = (home / ".config" / "goose" / ".served-scrub").read_text(encoding="utf-8")
    assert scrub.strip() == ""


def test_served_fails_clearly_when_required_env_missing(tmp_path: Path) -> None:
    """A missing required value fails non-zero — no leaky fallback."""
    env = {k: v for k, v in PLACEHOLDER_ENV.items() if k != "SERVED_MODEL_API_KEY"}
    proc = _run(tmp_path, env, "wire_nugget_served_env\n")
    assert proc.returncode == 64
    assert "misconfigured" in proc.stderr
    assert "SERVED_MODEL_API_KEY" in proc.stderr
