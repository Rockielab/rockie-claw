"""Tests for the binary self-update kill + warm-up (#1222 S4).

Two surfaces:

  1. ``Dockerfile.multitenant`` bakes ``DISABLE_AUTOUPDATER=1`` and
     ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` so claude/codex never
     self-update per machine — version is owned centrally via the image.
  2. ``warm_subscription_binary()`` in ``overlay/multitenant/entrypoint.sh``
     warms the chosen binary at machine start so the first user-facing
     call isn't a cold start. It must:
       - run in the background (never block broker startup),
       - export the autoupdater-off env so the warm ``--version`` cannot
         itself trigger a self-update,
       - be best-effort (a failing/missing binary never fails the
         container),
       - pick ``$BINARY`` (claude|codex), defaulting to claude.

Like the sibling render-settings test, we do NOT build the image — we
extract the function body and run it in a bash subprocess against a fake
binary on PATH.

Run from the repo root with:

    uv run --with pytest pytest \
      overlay/multitenant/tests/test_entrypoint_warm_binary.py -v
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = REPO_ROOT / "overlay" / "multitenant"
ENTRYPOINT = OVERLAY / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile.multitenant"


def _extract_function(name: str) -> str:
    src = ENTRYPOINT.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^{re.escape(name)}\(\)\s*\{{\n(?P<body>.*?)^\}}\n",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(src)
    assert m is not None, f"{name}() not found in entrypoint.sh"
    return m.group("body")


def _run_warm(
    tmp_path: Path,
    *,
    binary: str,
    make_claude: bool = True,
    make_codex: bool = False,
    claude_exits_nonzero: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run warm_subscription_binary() with a fake binary on PATH. The fake
    writes the env it observed to ``env_seen`` so we can assert the
    autoupdater-off vars were passed through."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env_seen = tmp_path / "env_seen.txt"

    def _write_fake(name: str, nonzero: bool) -> None:
        exit_line = "exit 7" if nonzero else 'echo "fake $0 2.1.0"; exit 0'
        (bindir / name).write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "--version" ]; then\n'
            f'  printf "AUTOUPDATER=%s NONESSENTIAL=%s\\n" '
            f'"$DISABLE_AUTOUPDATER" '
            f'"$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" > "{env_seen}"\n'
            f"  {exit_line}\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        (bindir / name).chmod(0o755)

    if make_claude:
        _write_fake("claude", claude_exits_nonzero)
    if make_codex:
        _write_fake("codex", False)

    body = _extract_function("warm_subscription_binary")
    harness = (
        'log() { printf "[entrypoint] %s\\n" "$*" >&2; }\n'
        # Re-wrap the extracted body into the named function, then call it.
        f"warm_subscription_binary() {{\n{body}\n}}\n"
        "warm_subscription_binary\n"
        # Wait for the backgrounded warm subshell so the test is
        # deterministic. In production the broker waits, not this script.
        "wait\n"
    )
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "BINARY": binary,
    }
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc, env_seen


def test_warm_runs_claude_and_disables_autoupdater(tmp_path: Path) -> None:
    proc, env_seen = _run_warm(tmp_path, binary="claude")
    assert proc.returncode == 0, proc.stderr
    assert "warming claude --version" in proc.stderr
    assert "claude warmed" in proc.stderr
    # The warm call must have passed the autoupdater-off env to the binary.
    assert env_seen.read_text(encoding="utf-8").strip() == (
        "AUTOUPDATER=1 NONESSENTIAL=1"
    )


def test_warm_respects_binary_codex(tmp_path: Path) -> None:
    proc, env_seen = _run_warm(
        tmp_path, binary="codex", make_claude=False, make_codex=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "warming codex --version" in proc.stderr
    assert env_seen.exists()


def test_warm_defaults_to_claude_for_unknown_binary(tmp_path: Path) -> None:
    proc, _ = _run_warm(tmp_path, binary="totally-bogus")
    assert proc.returncode == 0, proc.stderr
    assert "warming claude --version" in proc.stderr


def test_warm_is_nonfatal_when_binary_missing(tmp_path: Path) -> None:
    # No fake claude/codex on PATH at all.
    proc, _ = _run_warm(
        tmp_path, binary="claude", make_claude=False, make_codex=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipping warm-up" in proc.stderr


def test_warm_is_nonfatal_when_binary_errors(tmp_path: Path) -> None:
    proc, _ = _run_warm(tmp_path, binary="claude", claude_exits_nonzero=True)
    # A non-zero `--version` must NOT fail the entrypoint.
    assert proc.returncode == 0, proc.stderr
    assert "warm-up exited non-zero (non-fatal)" in proc.stderr


def test_entrypoint_calls_warm_in_subscription_mode() -> None:
    src = ENTRYPOINT.read_text(encoding="utf-8")
    # The subscription branch must invoke the warm step.
    sub_idx = src.index("MODE=subscription;")
    wait_idx = src.index('wait -n "$BROKER_PID"')
    warm_idx = src.index("warm_subscription_binary\n", sub_idx)
    # Warm is called inside the subscription branch, before the broker wait.
    assert sub_idx < warm_idx < wait_idx


def test_dockerfile_disables_self_updater() -> None:
    src = DOCKERFILE.read_text(encoding="utf-8")
    assert "ENV DISABLE_AUTOUPDATER=1" in src
    assert "ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in src
