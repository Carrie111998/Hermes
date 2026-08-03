"""At-rest permissions for the Chromium debug profile Hermes launches into.

``$HERMES_HOME/chrome-debug`` is a real Chromium user-data-dir — it is what
``--user-data-dir`` points at (``_chrome_debug_args``), so Cookies, Login Data
and Local Storage live there. Created with a bare ``os.makedirs`` it inherited
the umask and landed 0755.

``HERMES_HOME`` is 0700 by default, so default-config exposure is narrow and
this is defence in depth. The concrete scenario is the documented
``HERMES_HOME_MODE=0701`` hatch (letting nginx/caddy traverse HERMES_HOME to
reach a served subdirectory), where a 0755 child really is world-readable.

These tests pin the *contract* — no group/other bits on a freshly created
profile — rather than a frozen octal, and they run the real
``launch_chrome_debug`` path under a deliberately permissive ``umask 022`` so
a regression back to umask-derived modes fails here.

POSIX-only: mode bits are advisory on Windows, where at-rest protection is
ACL-based and ``os.chmod`` only toggles the read-only flag.
"""

import os
import stat

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits; Windows at-rest protection is ACL-based",
)

GROUP_OTHER_BITS = (
    stat.S_IRGRP
    | stat.S_IWGRP
    | stat.S_IXGRP
    | stat.S_IROTH
    | stat.S_IWOTH
    | stat.S_IXOTH
)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def permissive_umask():
    """Force a world-readable-by-default umask for the duration of a test."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Never let the ambient environment silently disable the hardening.
    monkeypatch.delenv("HERMES_CONTAINER", raising=False)
    monkeypatch.delenv("HERMES_SKIP_CHMOD", raising=False)
    monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    return home


@pytest.fixture
def no_browser_installed(monkeypatch):
    """Drive the real launch path with a binary that cannot spawn.

    ``launch_chrome_debug`` creates the profile dir before it tries any
    candidate, so a guaranteed-failing candidate exercises the real creation
    code without starting a browser on the test machine.
    """
    import hermes_cli.browser_connect as browser_connect

    monkeypatch.setattr(
        browser_connect,
        "get_chrome_debug_candidates",
        lambda system=None: ["/nonexistent/definitely-not-a-browser"],
    )


@pytest.fixture
def neutralized_secure_dir(monkeypatch):
    """Make the post-creation reconciliation step a no-op.

    ``_ensure_chrome_debug_data_dir`` hardens twice over: ``mode=`` on the
    ``makedirs`` call, then ``hermes_cli.config._secure_dir`` to reconcile
    policy. Either mechanism alone satisfies a plain "is it 0700?" assertion,
    so the two mask each other and a test that only checks the final mode
    cannot tell which one did the work — deleting ``mode=`` keeps every such
    test green.

    Stubbing the reconciler out isolates the creation mode as the *only*
    thing that can produce the observed bits. ``_secure_dir`` is patched on
    ``hermes_cli.config`` (not on the caller) because the production code
    imports it lazily inside the function, so the attribute is resolved at
    call time.
    """
    import hermes_cli.config as hermes_config

    calls = []

    def _recording_noop(path):
        calls.append(str(path))

    monkeypatch.setattr(hermes_config, "_secure_dir", _recording_noop)
    return calls


@pytest.fixture
def managed_nixos_home(hermes_home, monkeypatch):
    """Simulate a managed NixOS install that shares state via the hermes group.

    Reproduces the two conditions ``nix/nixosModules.nix`` actually creates:

    * ``systemd.tmpfiles`` pins ``stateDir/.hermes`` to ``2770`` — setgid,
      group-rwx — so the gateway and interactive ``hostUsers`` share it;
    * the service runs with ``UMask = "0007"``, commented "files created by
      the gateway should be group-writable so interactive users in the hermes
      group can read/write them".

    ``chrome-debug`` is *not* in those tmpfiles rules, so it is created
    lazily at runtime under exactly this umask — which is why the creation
    mode, not just the reconciliation, has to honour the carve-out.
    """
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    os.chmod(hermes_home, 0o2770)
    previous = os.umask(0o007)
    try:
        yield hermes_home
    finally:
        os.umask(previous)


def test_launch_creates_profile_dir_without_group_or_other_access(
    hermes_home, permissive_umask, no_browser_installed
):
    """The real launch path must not leave the profile world-readable."""
    from hermes_cli.browser_connect import chrome_debug_data_dir, launch_chrome_debug

    launch_chrome_debug(port=59991, system="Darwin")

    profile = hermes_home / "chrome-debug"
    assert profile.is_dir(), "launch must still create the user-data-dir"
    assert str(profile) == chrome_debug_data_dir()
    mode = _mode(profile)
    assert not (
        mode & GROUP_OTHER_BITS
    ), f"Chromium profile {profile} is group/other-accessible: {oct(mode)}"


def test_profile_mode_is_not_umask_derived(hermes_home, permissive_umask):
    """The mode must come from the code, not the ambient umask.

    Owner keeps full access (Chromium has to read and write its own profile)
    while group/other get nothing, which is only true if the mode was set
    deliberately at creation.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    mode = _mode(hermes_home / "chrome-debug")
    assert mode & stat.S_IRWXU == stat.S_IRWXU, "owner must retain full access"
    assert mode & GROUP_OTHER_BITS == 0, f"umask-derived mode leaked: {oct(mode)}"


def test_bare_makedirs_would_violate_the_contract(hermes_home, permissive_umask):
    """Guard against a vacuous suite.

    This is the exact pre-fix call shape. If it stops producing group/other
    bits under ``umask 022`` then the fixture — not the fix — is what the
    other tests are measuring, and they prove nothing.
    """
    legacy_shape = hermes_home / "chrome-debug-pre-fix-shape"
    os.makedirs(legacy_shape, exist_ok=True)

    mode = _mode(legacy_shape)
    assert mode & GROUP_OTHER_BITS, (
        "pre-fix call shape no longer leaks group/other bits under umask 022; "
        f"got {oct(mode)} — the permission tests here would be vacuous"
    )


def test_preexisting_world_readable_profile_is_healed(hermes_home, permissive_umask):
    """An older Hermes already left 0755 on disk; the next launch tightens it.

    The exposure this fixes is retroactive, so creation-time hardening alone
    would leave every existing install exposed. Reconciling is safe against a
    *running* Chromium: only group/other bits drop, the owner keeps ``rwx``,
    and POSIX evaluates the mode at ``open()`` rather than on already-open
    descriptors — see ``test_running_browser_keeps_profile_access``.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    profile = hermes_home / "chrome-debug"
    profile.mkdir()
    os.chmod(profile, 0o755)
    assert _mode(profile) & stat.S_IROTH, "fixture precondition: starts world-readable"

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    assert not (
        _mode(profile) & GROUP_OTHER_BITS
    ), f"pre-existing profile left exposed: {oct(_mode(profile))}"


def test_running_browser_keeps_profile_access(hermes_home, permissive_umask):
    """Tightening a live profile must not break the browser attached to it.

    Stands in for a running Chromium: an open descriptor on a profile store
    plus a fresh file created after the tighten. Both must succeed, because
    the owner's bits are untouched.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    profile = hermes_home / "chrome-debug"
    profile.mkdir()
    os.chmod(profile, 0o755)
    cookies = profile / "Cookies"
    fd = os.open(str(cookies), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"session-before")

        _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

        os.write(fd, b"|session-after")
    finally:
        os.close(fd)

    assert cookies.read_bytes() == b"session-before|session-after"
    # A new tab writing a new store must still work.
    (profile / "Local Storage").write_bytes(b"new-store")
    assert (profile / "Local Storage").read_bytes() == b"new-store"
    assert sorted(p.name for p in profile.iterdir()) == ["Cookies", "Local Storage"]


def test_launch_still_writes_its_stderr_log(
    hermes_home, permissive_umask, no_browser_installed
):
    """A permission fix that breaks the feature is the wrong fix.

    ``launch_chrome_debug`` opens a stderr log *inside* the profile dir to
    capture why a candidate died. Tightening the directory must not make that
    unwritable, or every launch diagnostic is lost.
    """
    from hermes_cli.browser_connect import _LAUNCH_STDERR_LOG, launch_chrome_debug

    result = launch_chrome_debug(port=59992, system="Darwin")

    assert result.launched is False, "no real browser should have started"
    assert (hermes_home / "chrome-debug" / _LAUNCH_STDERR_LOG).exists()


def test_launch_stderr_log_is_written_owner_only(
    hermes_home, permissive_umask, no_browser_installed
):
    """The launch log must not land 0644 inside the hardened profile dir.

    Unlike the uuid-named scratch files elsewhere, ``launch-stderr.log`` has a
    fixed, guessable name — so under ``HERMES_HOME_MODE=0701`` (directory
    traversable but unlistable) it is the one file in here an unrelated local
    account could actually open. It carries Chromium's stderr, which routinely
    includes the profile path and extension/keychain diagnostics.
    """
    from hermes_cli.browser_connect import _LAUNCH_STDERR_LOG, launch_chrome_debug

    launch_chrome_debug(port=59994, system="Darwin")

    log = hermes_home / "chrome-debug" / _LAUNCH_STDERR_LOG
    mode = _mode(log)
    assert not (
        mode & GROUP_OTHER_BITS
    ), f"launch log {log} is group/other-readable: {oct(mode)}"


def test_plain_open_of_stderr_log_would_violate_the_contract(
    hermes_home, permissive_umask
):
    """Vacuity guard for the log-mode contract.

    ``open(path, "wb")`` is the exact pre-fix call shape. If it stops leaking
    group/other bits under ``umask 022``, the assertion above proves nothing.
    """
    profile = hermes_home / "chrome-debug"
    profile.mkdir(mode=0o700)
    legacy_shape = profile / "pre-fix-shape.log"
    with open(legacy_shape, "wb") as fh:
        fh.write(b"chromium stderr")

    mode = _mode(legacy_shape)
    assert mode & GROUP_OTHER_BITS, (
        "pre-fix open() shape no longer leaks group/other bits under umask 022; "
        f"got {oct(mode)} — the log-mode test would be vacuous"
    )


def test_stderr_log_is_truncated_per_candidate(hermes_home, permissive_umask):
    """Hardening the log must not turn the overwrite into an append.

    ``launch_chrome_debug`` reuses one log path across candidates and reads the
    tail back to explain a failure. If a second open appended instead of
    truncating, the reported tail would be the *previous* candidate's stderr.
    """
    from hermes_cli.browser_connect import _open_launch_stderr_log, _read_stderr_tail

    profile = hermes_home / "chrome-debug"
    profile.mkdir(mode=0o700)
    log = profile / "launch-stderr.log"

    with _open_launch_stderr_log(str(log)) as fh:
        fh.write(b"first-candidate-stderr-and-longer")
    with _open_launch_stderr_log(str(log)) as fh:
        fh.write(b"second")

    assert log.read_bytes() == b"second", "O_TRUNC lost: log now appends"
    assert _read_stderr_tail(str(log)) == "second", "diagnostic tail must still read"
    assert not (_mode(log) & GROUP_OTHER_BITS), "reopen must not widen the mode"


def test_managed_mode_leaves_existing_permissions_alone(
    hermes_home, permissive_umask, monkeypatch
):
    """Managed/NixOS installs own their own modes; we must not fight them.

    The NixOS activation script deliberately sets group-readable modes so
    interactive users in the hermes group can share state with the gateway
    service. Delegating to the house helper inherits that carve-out.
    """
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    profile = hermes_home / "chrome-debug"
    profile.mkdir()
    os.chmod(profile, 0o750)

    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    assert _mode(profile) == 0o750, "managed-mode permissions overridden"


def test_creation_mode_alone_yields_owner_only(hermes_home, neutralized_secure_dir):
    """``mode=`` on the ``makedirs`` call must be doing real work by itself.

    Two mechanisms harden this directory and either one satisfies a bare
    "is it 0700?" check, so they mask each other: with ``_secure_dir`` in play
    every mode assertion in this file stays green even if ``mode=`` is deleted.
    Here the reconciler is stubbed to a no-op, so the only thing that can
    produce owner-only bits is the mode passed at creation.

    This is the TOCTOU guarantee made observable. The window that ``mode=``
    closes — the instant between ``mkdir`` and a follow-up ``chmod`` where the
    profile sits world-readable — cannot be asserted by racing creation, but
    "correct without any chmod at all" is exactly equivalent and is
    deterministic.

    Deliberately not stacked with ``permissive_umask``: the umask is left
    alone so a 0700 result cannot be an accident of a restrictive ambient
    umask on the machine running the suite. See
    ``test_creation_mode_is_not_inherited_from_a_permissive_umask``.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    profile = hermes_home / "chrome-debug"
    assert profile.is_dir()
    assert neutralized_secure_dir == [
        str(profile)
    ], "reconciler stub was not exercised; this test is not isolating creation"
    mode = _mode(profile)
    assert mode & GROUP_OTHER_BITS == 0, (
        "with the reconciler neutralized the profile is group/other-accessible "
        f"({oct(mode)}) — the mode= on makedirs is not being applied, so the "
        "mkdir->chmod TOCTOU window is open"
    )
    assert mode & stat.S_IRWXU == stat.S_IRWXU, "owner must retain full access"


def test_creation_mode_is_not_inherited_from_a_permissive_umask(
    hermes_home, permissive_umask, neutralized_secure_dir
):
    """Same isolation, with the umask forced wide open.

    Together with the test above this pins the claim precisely: 0700 comes
    from the ``mode=`` argument, not from the ambient umask and not from the
    reconciler. ``os.makedirs`` masks its ``mode`` with the umask, so a
    ``mode=`` of 0700 under ``umask 022`` still yields 0700 — but a *missing*
    ``mode=`` yields 0755 and fails here.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    mode = _mode(hermes_home / "chrome-debug")
    assert neutralized_secure_dir, "reconciler stub was not exercised"
    assert mode & GROUP_OTHER_BITS == 0, (
        f"umask-derived mode leaked with the reconciler neutralized: {oct(mode)}"
    )


def test_managed_mode_fresh_profile_keeps_group_sharing(managed_nixos_home):
    """A *newly created* profile on NixOS must stay group-shareable.

    The sibling test above covers a pre-existing directory, where
    ``_secure_dir``'s ``is_managed()`` early return is enough. This is the
    other managed path: the directory does not exist yet, so the mode passed
    at *creation* is the only thing that decides it — and ``chrome-debug`` is
    not among the directories ``nix/nixosModules.nix`` pre-creates via
    ``systemd.tmpfiles``, so on a managed host it is always this path.

    Forcing 0700 here would not merely be cosmetic. The module shares
    ``$HERMES_HOME`` between the gateway service and interactive ``hostUsers``
    through the hermes group (``2770`` + ``UMask = "0007"`` + a deliberate
    refusal to ``chown -R``, which would strip setgid). A 0700 profile created
    by whichever side runs first locks the other out of the browser entirely
    with EACCES — a permission "fix" that destroys the feature it protects.
    """
    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    profile = managed_nixos_home / "chrome-debug"
    assert not profile.exists(), "fixture precondition: dir must be absent"

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    assert profile.is_dir(), "managed mode must still create the profile dir"
    mode = _mode(profile)
    assert mode & stat.S_IRWXG, (
        f"fresh managed-mode profile dropped group access ({oct(mode)}); the "
        "NixOS module's hermes-group sharing (2770 + UMask=0007) is broken, so "
        "an interactive hostUsers CLI and the gateway can no longer share it"
    )


def test_home_mode_override_is_honored(hermes_home, permissive_umask, monkeypatch):
    """The documented web-server traversal hatch still applies.

    Delegating to the house helper keeps one source of truth for this knob
    instead of hardcoding a mode this module invented.
    """
    monkeypatch.setenv("HERMES_HOME_MODE", "0701")

    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    mode = _mode(hermes_home / "chrome-debug")
    assert mode == 0o701
    # Execute-only: traversal without a directory listing.
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH)), "listing must stay closed"


def test_container_deployments_still_get_a_usable_profile(
    hermes_home, permissive_umask, monkeypatch
):
    """A container/volume-mount deployment must not crash or lose the dir.

    Note what this does and does not claim. ``_secure_dir`` checks
    ``is_managed()`` only — unlike ``_secure_file`` it does **not** consult
    ``_is_container()``, so the profile dir is still tightened to 0700 here.
    That is fine rather than a carve-out being violated: ``ensure_hermes_home``
    already puts HERMES_HOME and every standard subdir at 0700 in a container,
    so a 0700 child adds no new restriction. Multi-UID deployments are served
    by ``HERMES_UID``/``HERMES_GID`` or ``HERMES_HOME_MODE``, both of which
    ``_secure_dir`` honours. So this asserts the weaker, true contract: the
    directory still exists and nothing raised.
    """
    monkeypatch.setenv("HERMES_CONTAINER", "1")
    monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")

    from hermes_cli.browser_connect import (
        _ensure_chrome_debug_data_dir,
        chrome_debug_data_dir,
    )

    _ensure_chrome_debug_data_dir(chrome_debug_data_dir())

    assert (hermes_home / "chrome-debug").is_dir()
