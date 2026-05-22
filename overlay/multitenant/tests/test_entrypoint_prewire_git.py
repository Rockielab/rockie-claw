"""Regression test for ``prewire_git_credentials()`` in ``entrypoint.sh``.

rockie-workspace#575: the first attempt (PR #22) wired the git identity
and the github.com / huggingface.co credential helpers with
``git config --global``. On the live runtime ``--global`` lands in the
runtime user's ``~/.gitconfig`` (``/home/runtime/.gitconfig``), but the
broker ``/spawn`` + ``/ws`` PTY spawn the agent shell as **root** — so
the agent's git never reads any of it. The Pass-1 dogfood failed:
``git commit`` and ``git push`` to huggingface.co still broke.

The fix switches every write to ``git config --system`` (→
``/etc/gitconfig``), which is user-independent and IS visible to the
root-spawned agent, exactly how the pre-existing
``git-credential-rockie.sh`` is wired by ``Dockerfile.multitenant``.

This test pins that contract. It extracts the ``prewire_git_credentials``
function body from the live ``entrypoint.sh`` and runs it inside a bash
subprocess with ``GIT_CONFIG_SYSTEM`` redirected at a tmp file (so
``git config --system`` works without root), a fake ``gh`` on PATH, and
a fake HF helper script. It then asserts:

  * identity + both helpers land in the **system** scope file, NOT in a
    ``--global`` ``~/.gitconfig``;
  * the github.com helper is the ``gh auth git-credential`` form;
  * the no-tokens / HF-only / pre-existing-identity / git-missing paths
    behave (conditional, no clobber, no hard-fail).

Run from the repo root with:

    uv run --with pytest pytest \
      overlay/multitenant/tests/test_entrypoint_prewire_git.py -v
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO_ROOT / "overlay" / "multitenant" / "entrypoint.sh"


def _extract_prewire_fn() -> str:
    """Return the body of ``prewire_git_credentials`` from entrypoint.sh."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^prewire_git_credentials\(\)\s*\{\n(?P<body>.*?)^\}\n",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(src)
    assert m is not None, "prewire_git_credentials() not found in entrypoint.sh"
    return m.group("body")


def _run_prewire(
    tmp_path: Path,
    env_overrides: dict[str, str],
    *,
    with_gh: bool = True,
    with_hf_helper: bool = True,
    git_on_path: bool = True,
    seed_system: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Spawn bash running just ``prewire_git_credentials``.

    Returns (completed_process, system_config_path, global_config_path).

    ``git config --system`` is redirected to a tmp file via
    ``GIT_CONFIG_SYSTEM``; ``--global`` is redirected to a separate tmp
    file via ``HOME`` so the test can assert nothing leaked to global.
    ``seed_system`` pre-populates the system config file before the
    function runs (used to prove the no-clobber identity guard).
    """
    sysconf = tmp_path / "system.gitconfig"
    sysconf.write_text(seed_system, encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # Fake bin dir: a stub `gh` that prints nothing, plus optionally a
    # symlink to the real `git`. The HF helper path the entrypoint wires
    # is the literal /usr/local/bin/git-credential-hf-env.sh — we cannot
    # create that without root, so we rewrite it to a tmp path below.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if with_gh:
        gh_stub = bindir / "gh"
        gh_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gh_stub.chmod(0o755)

    hf_helper = tmp_path / "git-credential-hf-env.sh"
    if with_hf_helper:
        hf_helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hf_helper.chmod(0o755)

    body = _extract_prewire_fn()
    # Rewrite the baked-in HF helper path to our tmp stub so the
    # `[ -x "$hf_helper" ]` guard can pass without root.
    body = body.replace("/usr/local/bin/git-credential-hf-env.sh", str(hf_helper))

    script = (
        "set -euo pipefail\n"
        "log() { printf '[entrypoint] %s\\n' \"$*\" >&2; }\n"
        "prewire_git_credentials() {\n"
        f"{body}"
        "}\n"
        "prewire_git_credentials\n"
    )

    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()
    real_bash = subprocess.run(
        ["sh", "-c", "command -v bash"], capture_output=True, text=True
    ).stdout.strip()
    if git_on_path:
        # Symlink the real git into our controlled bindir.
        (bindir / "git").symlink_to(real_git)
        path = f"{bindir}:/usr/bin:/bin"
    else:
        # No git anywhere on PATH — exercise the missing-git guard.
        # bash still has to be reachable, so symlink just bash in.
        (bindir / "bash").symlink_to(real_bash)
        path = str(bindir)

    env: dict[str, str] = {
        "PATH": path,
        "HOME": str(fake_home),
        "GIT_CONFIG_SYSTEM": str(sysconf),
        # Belt-and-suspenders: pin --global at the fake home too.
        "GIT_CONFIG_GLOBAL": str(fake_home / ".gitconfig"),
    }
    env.update(env_overrides)

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, sysconf, (fake_home / ".gitconfig")


def _git_get(config_path: Path, key: str, *, all_values: bool = False) -> str:
    """Read a key out of a specific git config file."""
    if not config_path.exists():
        return ""
    flag = "--get-all" if all_values else "--get"
    return subprocess.run(
        ["git", "config", "--file", str(config_path), flag, key],
        capture_output=True,
        text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Core regression: both-tokens present → identity + helpers land in --system
# ---------------------------------------------------------------------------


def test_both_tokens_writes_identity_and_helpers_to_system(tmp_path: Path) -> None:
    result, sysconf, globalconf = _run_prewire(
        tmp_path,
        {"GH_TOKEN": "ghp_faketoken", "HF_TOKEN": "hf_faketoken"},
    )
    assert result.returncode == 0, result.stderr

    # Identity lands in the SYSTEM file (the whole point of the #575 rework).
    assert _git_get(sysconf, "user.name") == "Rockie Agent"
    assert _git_get(sysconf, "user.email") == "agent@rockielab.com"

    # github.com helper is the gh-credential form, in --system.
    gh_helpers = _git_get(
        sysconf, "credential.https://github.com.helper", all_values=True
    )
    assert "auth git-credential" in gh_helpers, gh_helpers

    # huggingface.co helper points at the static env-backed helper, in --system.
    hf_helper = _git_get(sysconf, "credential.https://huggingface.co.helper")
    assert hf_helper.endswith("git-credential-hf-env.sh"), hf_helper

    # Nothing leaked to --global ~/.gitconfig — that file is what the
    # root-spawned agent can NOT see, so it must stay empty.
    assert not globalconf.exists() or globalconf.read_text().strip() == "", (
        "prewire must not write --global; the agent runs as root and "
        "would never read /home/runtime/.gitconfig"
    )


# ---------------------------------------------------------------------------
# github.com helper is the deterministic empty-reset + add idiom
# ---------------------------------------------------------------------------


def test_github_helper_uses_reset_then_add_idiom(tmp_path: Path) -> None:
    """`gh auth setup-git` writes an empty helper (resets inherited helpers)
    then the real one, for both github.com and gist.github.com. The
    hand-rolled --system equivalent must match for each host."""
    result, sysconf, _ = _run_prewire(
        tmp_path, {"GH_TOKEN": "ghp_faketoken"}
    )
    assert result.returncode == 0, result.stderr
    for host in ("github.com", "gist.github.com"):
        helpers = subprocess.run(
            [
                "git", "config", "--file", str(sysconf),
                "--get-all", f"credential.https://{host}.helper",
            ],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        # First entry empty (the reset), last entry the gh helper.
        assert helpers and helpers[0] == "", (host, helpers)
        assert helpers[-1].startswith("!"), (host, helpers)
        assert "auth git-credential" in helpers[-1], (host, helpers)


# ---------------------------------------------------------------------------
# Conditional behavior: no tokens / HF-only
# ---------------------------------------------------------------------------


def test_no_tokens_writes_identity_only(tmp_path: Path) -> None:
    """BYOK / pre-connection: no GH_TOKEN, no HF_TOKEN. Identity is still
    set (harmless, lets ad-hoc commits work) but no helpers are wired."""
    result, sysconf, _ = _run_prewire(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert _git_get(sysconf, "user.name") == "Rockie Agent"
    assert _git_get(sysconf, "credential.https://github.com.helper") == ""
    assert _git_get(sysconf, "credential.https://huggingface.co.helper") == ""


def test_hf_only_skips_github_helper(tmp_path: Path) -> None:
    result, sysconf, _ = _run_prewire(tmp_path, {"HF_TOKEN": "hf_faketoken"})
    assert result.returncode == 0, result.stderr
    assert _git_get(sysconf, "credential.https://huggingface.co.helper") != ""
    assert _git_get(sysconf, "credential.https://github.com.helper") == ""


# ---------------------------------------------------------------------------
# No-clobber: a pre-existing identity must not be overwritten
# ---------------------------------------------------------------------------


def test_preexisting_identity_not_clobbered(tmp_path: Path) -> None:
    """If the tenant already set a git identity at any scope, the default
    Rockie Agent identity must NOT overwrite it. The function's guard
    reads `git config user.name` (merged config), so an identity in the
    system file must suppress the default write."""
    result, sysconf, _ = _run_prewire(
        tmp_path,
        {"GH_TOKEN": "ghp_faketoken"},
        seed_system=(
            "[user]\n\tname = Existing Person\n"
            "\temail = existing@example.com\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert _git_get(sysconf, "user.name") == "Existing Person"
    assert _git_get(sysconf, "user.email") == "existing@example.com"


# ---------------------------------------------------------------------------
# Failure modes: never hard-fail the entrypoint
# ---------------------------------------------------------------------------


def test_git_missing_does_not_fail(tmp_path: Path) -> None:
    """git absent from PATH: log a WARN, return 0 — the runtime must boot."""
    result, _, _ = _run_prewire(
        tmp_path, {"GH_TOKEN": "ghp_x"}, git_on_path=False
    )
    assert result.returncode == 0, result.stderr
    assert "git not on PATH" in result.stderr


def test_gh_missing_with_token_does_not_fail(tmp_path: Path) -> None:
    """GH_TOKEN set but gh CLI absent: warn, skip github wire, return 0."""
    result, sysconf, _ = _run_prewire(
        tmp_path, {"GH_TOKEN": "ghp_x"}, with_gh=False
    )
    assert result.returncode == 0, result.stderr
    assert "gh CLI missing" in result.stderr
    assert _git_get(sysconf, "credential.https://github.com.helper") == ""


def test_hf_helper_missing_with_token_does_not_fail(tmp_path: Path) -> None:
    """HF_TOKEN set but the baked helper script absent: warn, return 0."""
    result, sysconf, _ = _run_prewire(
        tmp_path, {"HF_TOKEN": "hf_x"}, with_hf_helper=False
    )
    assert result.returncode == 0, result.stderr
    assert "not present" in result.stderr
    assert _git_get(sysconf, "credential.https://huggingface.co.helper") == ""
