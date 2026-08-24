"""The dev-sandbox scripts must not invoke a PATH-resolved ``env``.

Same hazard #88385 fixed in install.sh: uv's installer writes
``~/.local/bin/env`` -- a PATH helper meant to be *sourced*, which ignores
its arguments and exits 0 -- and puts ``~/.local/bin`` ahead of /usr/bin.
On any machine where that shim exists, ``env VAR=... cmd`` resolves to it,
runs nothing, and reports success.

dev-sandbox.sh launched its entire stage 2 through exactly that shape
(``env DEV_SANDBOX_...=... unshare ...``), so on a shim-bearing developer
machine every sandbox invocation printed its banner, executed nothing, and
exited 0 -- including the Install & Update E2E driver, which then failed
with the payload's output nowhere to be found.

The scan here differs from install.sh's in one way the sandbox scripts
need: the offending call spelled ``env`` on its own line with the variables
on backslash-continued lines, so continuations are joined before matching.
"""

import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_SCRIPTS = [
    REPO_ROOT / "scripts" / "dev-sandbox.sh",
    REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh",
    REPO_ROOT / "scripts" / "sandbox" / "pick-release-tags.sh",
    REPO_ROOT / "tests" / "install" / "install-update-e2e.sh",
]

# `env FOO=bar cmd` (or bare `env` before continued assignments) where env is
# resolved through PATH. Excludes /usr/bin/env and other absolute or relative
# paths, and words merely ending in "env" (--setenv, printenv).
BARE_ENV_PREFIX = re.compile(r"(?:^|[^./\w-])env\s+[A-Z_]+=")
COMMENT = re.compile(r"(?:^|\s)#.*$")


def _logical_lines(text: str) -> list[str]:
    """Comment-stripped lines with backslash continuations joined."""
    joined = re.sub(r"\\\n", " ", text)
    return [COMMENT.sub("", line) for line in joined.splitlines()]


def test_no_path_resolved_env_in_sandbox_scripts() -> None:
    """No sandbox-side script may set variables via a PATH-resolved `env`."""
    offenders = [
        (script.name, number, line.rstrip())
        for script in SANDBOX_SCRIPTS
        for number, line in enumerate(
            _logical_lines(script.read_text(encoding="utf-8")), 1
        )
        if BARE_ENV_PREFIX.search(line)
    ]
    assert not offenders, (
        "sandbox scripts must not prefix commands with a PATH-resolved "
        "`env` -- a shadowing ~/.local/bin/env (written by uv) swallows the "
        "command and returns 0. Use bash prefix assignments "
        "(`VAR=... cmd`) instead. Offending logical lines: " + repr(offenders)
    )


def _write_env_shim(directory: Path) -> None:
    """Drop in a no-op `env` matching the one uv installs."""
    shim = directory / "env"
    shim.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            # add binaries to PATH if they aren't added yet
            case ":${PATH}:" in
                *:"$HOME/.local/bin":*) ;;
                *) export PATH="$HOME/.local/bin:$PATH" ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)


def test_prefix_assignments_survive_the_env_shim(tmp_path: Path) -> None:
    """Behavioural proof: with the shim first on PATH, the old launch shape
    silently succeeds without running anything; bash prefix assignments both
    run the command and deliver the variables."""
    _write_env_shim(tmp_path)
    path = f"{tmp_path}:/usr/bin:/bin"

    def run(script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", script],
            env={"PATH": path, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )

    # The old shape: `false` never runs, yet the caller sees success -- a
    # stage 2 launched this way vanishes without an error.
    assert run("env FOO=bar false").returncode == 0

    # The new shape: the command really runs (failure propagates)...
    assert run("FOO=bar false").returncode != 0
    # ...and the environment still arrives, including for multi-variable,
    # continuation-style prefixes like the stage 2 launch uses.
    delivered = run('FOO=bar \\\n  BAR=baz \\\n  sh -c \'printf "%s %s" "$FOO" "$BAR"\'')
    assert delivered.stdout == "bar baz"
