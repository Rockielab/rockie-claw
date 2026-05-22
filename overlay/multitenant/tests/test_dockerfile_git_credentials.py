"""Regression test for the build-time git credential + identity RUN block.

rockie-workspace#575 / platform-runtime#24: git credential-helper and
default-identity registration used to be an entrypoint-time shell
function (``prewire_git_credentials()`` in ``entrypoint.sh``). That ran
AFTER the ``USER runtime`` directive, so it executed as the unprivileged
``runtime`` user and could not ``git config --system`` into the
root-owned ``/etc/gitconfig``. The broker ``/spawn`` + ``/ws`` PTY spawn
the agent shell as **root**, which only ever reads ``/etc/gitconfig`` —
so the entrypoint wiring was invisible to the agent's git.

The fix moves the wiring to a build-time ``RUN`` block in
``Dockerfile.multitenant`` that runs as root, *before* ``USER runtime``,
so the writes land in the image's ``/etc/gitconfig`` and persist for
every spawned shell.

This test pins that contract. It extracts the credential/identity
``RUN`` block straight out of the live ``Dockerfile.multitenant``, runs
it in a bash subprocess with ``GIT_CONFIG_SYSTEM`` redirected at a tmp
file (so ``git config --system`` works without root) and a fake ``gh``
on PATH (the block does ``$(command -v gh)``), then asserts all four
config groups land:

  * ``credential.https://github.com.helper`` — reset-then-add idiom;
  * ``credential.https://gist.github.com.helper`` — same idiom;
  * ``credential.https://huggingface.co.helper`` — the static HF helper;
  * ``user.name`` / ``user.email`` — the default Rockie Agent identity.

Run from the repo root with:

    uv run --with pytest pytest \
      overlay/multitenant/tests/test_dockerfile_git_credentials.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "Dockerfile.multitenant"

# The line that uniquely identifies the credential/identity RUN block.
_BLOCK_MARKER = 'git config --system user.name "Rockie Agent"'


def _extract_credential_run_block() -> str:
    """Return the credential/identity ``RUN`` block as a single shell script.

    Locates the logical ``RUN`` command whose body contains the marker
    line, then joins its ``\\``-continued physical lines into one script
    with the leading ``RUN `` stripped.
    """
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()

    # Walk the file, grouping physical lines into logical commands by
    # following trailing-backslash continuations.
    i = 0
    while i < len(lines):
        if lines[i].startswith("RUN "):
            block: list[str] = [lines[i]]
            while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                block.append(lines[i])
            if any(_BLOCK_MARKER in line for line in block):
                joined = "\n".join(block)
                # Strip the leading `RUN ` from the first physical line.
                assert joined.startswith("RUN "), joined
                return joined[len("RUN ") :]
        i += 1

    raise AssertionError(
        f"credential/identity RUN block not found in {DOCKERFILE}"
    )


def _run_credential_block(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the extracted RUN block in bash; return (process, system config).

    ``git config --system`` is redirected to a tmp file via
    ``GIT_CONFIG_SYSTEM``; ``--global`` is pinned at a tmp path via
    ``GIT_CONFIG_GLOBAL`` so nothing can leak there. A stub ``gh`` is
    placed on PATH because the block resolves it with ``command -v gh``.
    """
    sysconf = tmp_path / "system.gitconfig"
    sysconf.write_text("", encoding="utf-8")
    globalconf = tmp_path / "global.gitconfig"

    # Fake bin dir: a stub `gh` (the block does `$(command -v gh)`), plus
    # the real `git` symlinked in so it stays reachable.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh_stub = bindir / "gh"
    gh_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh_stub.chmod(0o755)

    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()
    (bindir / "git").symlink_to(real_git)

    block = _extract_credential_run_block()
    script = "set -euo pipefail\n" + block + "\n"

    env: dict[str, str] = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GIT_CONFIG_SYSTEM": str(sysconf),
        "GIT_CONFIG_GLOBAL": str(globalconf),
    }
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, sysconf


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
# Core contract: all four config groups land in the --system scope
# ---------------------------------------------------------------------------


def test_build_block_writes_all_four_groups_to_system(tmp_path: Path) -> None:
    result, sysconf = _run_credential_block(tmp_path)
    assert result.returncode == 0, result.stderr

    # Identity lands in the SYSTEM file (the whole point of the #575 rework).
    assert _git_get(sysconf, "user.name") == "Rockie Agent"
    assert _git_get(sysconf, "user.email") == "agent@rockielab.com"

    # github.com helper is the gh-credential form, in --system.
    gh_helpers = _git_get(
        sysconf, "credential.https://github.com.helper", all_values=True
    )
    assert "auth git-credential" in gh_helpers, gh_helpers

    # gist.github.com is wired the same way.
    gist_helpers = _git_get(
        sysconf, "credential.https://gist.github.com.helper", all_values=True
    )
    assert "auth git-credential" in gist_helpers, gist_helpers

    # huggingface.co helper points at the static env-backed helper, in --system.
    hf_helper = _git_get(sysconf, "credential.https://huggingface.co.helper")
    assert hf_helper.endswith("git-credential-hf-env.sh"), hf_helper


# ---------------------------------------------------------------------------
# github.com + gist.github.com helpers use the empty-reset + add idiom
# ---------------------------------------------------------------------------


def test_github_helpers_use_reset_then_add_idiom(tmp_path: Path) -> None:
    """`gh auth setup-git` writes an empty helper (resets inherited
    helpers) then the real one, for both github.com and gist.github.com.
    The hand-rolled --system equivalent must match for each host."""
    result, sysconf = _run_credential_block(tmp_path)
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
