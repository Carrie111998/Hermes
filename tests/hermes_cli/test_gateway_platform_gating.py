"""Host-specific gating in ``hermes_cli.gateway._all_platforms()``.

Some messaging platforms can't function on every host. The gate lives
in one place — ``_all_platforms()`` — so the setup wizard, the curses
gateway-config menu, and any future picker all see the same filtered
list.

Currently:
- Matrix is hidden on Windows. The ``[matrix]`` extra pulls
  ``mautrix[encryption]`` -> ``python-olm``, which has no Windows wheel
  and needs ``make`` + libolm to build from sdist. There's no native
  Windows path that works.
"""


# Every built-in adapter under gateway/platforms/ that is expected to have
# a picker entry today. This intentionally excludes dingtalk/feishu/wecom/
# wecom_callback — those were dropped by the same e39b468 refactor but are
# tracked and restored separately (see .plans/missing-platform-plugin-shims.md
# and the PR that follows it); asserting them here would make this test
# depend on merge order between unrelated PRs.
BUILTIN_ADAPTER_KEYS = frozenset(
    {
        "telegram",
        "slack",
        "matrix",
        "whatsapp",
        "email",
        "sms",
        "mattermost",
        "signal",
        "weixin",
        "bluebubbles",
        "qqbot",
        "yuanbao",
    }
)


class TestBuiltinAdaptersStayInPicker:
    def test_every_builtin_adapter_key_is_in_all_platforms(self, monkeypatch):
        """Regression guard for e39b468-style silent deletions.

        A future refactor that deletes a _PLATFORMS entry (or claims a
        built-in "moved to plugins" without actually creating the plugin)
        should fail CI here instead of shipping a picker that's silently
        missing a platform.
        """
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        keys = {p["key"] for p in gateway_mod._all_platforms()}
        missing = BUILTIN_ADAPTER_KEYS - keys
        assert not missing, (
            f"Built-in adapter(s) {sorted(missing)} have no _all_platforms() "
            "entry — they exist in gateway/platforms/ but are invisible to "
            "`hermes setup gateway`."
        )

    def test_telegram_slack_matrix_use_bespoke_setup_fn(self, monkeypatch):
        """Matrix's "leave the token empty for password login" path is real.

        The generic _setup_standard_platform() fallback treats the first
        `vars` entry (token_var) as mandatory and aborts the whole wizard
        if left empty — which would silently break Matrix's documented
        password-login path. These three must resolve to a bespoke setup
        function, not fall through to the generic flow.

        Where that function lives moved in #41112: it used to be
        ``hermes_cli.setup._setup_<key>``, reached via
        ``_builtin_setup_fn()``; it is now ``interactive_setup`` registered
        on the plugin's registry entry by
        ``plugins/platforms/<key>/adapter.py::register()``. The invariant is
        unchanged — ``_configure_platform()`` tries the registry entry's
        ``setup_fn`` *first*, so a non-None ``setup_fn`` is exactly what
        keeps these three off the generic path. Assert that, not the
        retired lookup, or this guard passes on a mechanism nothing uses.
        """
        import hermes_cli.gateway as gateway_mod

        # matrix is gated off Windows; pin the host so the entry exists.
        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        by_key = {p["key"]: p for p in gateway_mod._all_platforms()}

        for key in ("telegram", "slack", "matrix"):
            platform = by_key.get(key)
            assert platform is not None, f"{key} is missing from the picker"

            entry = platform.get("_registry_entry")
            builtin = gateway_mod._builtin_setup_fn(key)
            assert (entry is not None and entry.setup_fn is not None) or builtin is not None, (
                f"{key} resolves to neither a plugin setup_fn nor a built-in "
                "setup function, so _configure_platform() falls through to "
                "_setup_standard_platform() — which aborts the wizard when the "
                "first `vars` entry is left empty."
            )


class TestMatrixHiddenOnWindows:
    def test_matrix_present_on_linux(self, monkeypatch):
        """Sanity: matrix is still in the picker on Linux/macOS."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" in keys, "matrix must be available on Linux"


    def test_other_platforms_unaffected_on_windows(self, monkeypatch):
        """Gating must only drop matrix, not collateral damage."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "win32")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        # A representative sample of platforms that have no Windows
        # blockers — picker should still surface them.
        for must_have in ("telegram", "discord", "slack", "mattermost"):
            assert must_have in keys, (
                f"{must_have} disappeared from Windows picker — gate is "
                "over-filtering"
            )
