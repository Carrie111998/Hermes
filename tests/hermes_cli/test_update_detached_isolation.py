"""The detached `/update` must leave the gateway's supervisor cgroup.

`setsid` creates a new session but NOT a new cgroup: a detached updater
spawned from a systemd-supervised gateway stays inside
``hermes-gateway.service``, so the moment the update restarts that unit
(or the unit is stopped for any other reason) systemd kills the updater
mid-mutation.  The updater must run in its own transient scope.

The updater itself refuses to mutate when it is not isolated, so this is
also what keeps the supported `/update` route working.
"""

from __future__ import annotations

from hermes_cli import update_quiesce


class TestScopeWrapping:
    def test_available_systemd_run_wraps_the_command_in_its_own_scope(self):
        argv = update_quiesce.isolated_updater_command(
            ["hermes", "update", "--gateway"],
            systemd_run="/usr/bin/systemd-run",
            unit_name="hermes-updater-123",
        )
        assert argv[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]
        assert "--unit" in argv and "hermes-updater-123" in argv
        assert "--collect" in argv
        assert argv[-3:] == ["hermes", "update", "--gateway"]
        assert argv[argv.index("hermes") - 1] == "--"

    def test_without_systemd_run_the_command_is_unchanged(self):
        base = ["hermes", "update", "--gateway"]
        assert update_quiesce.isolated_updater_command(
            base, systemd_run=None, unit_name="hermes-updater-123"
        ) == base

    def test_shell_prefix_is_quoted_and_ends_with_a_separator(self):
        prefix = update_quiesce.isolated_updater_shell_prefix(
            systemd_run="/usr/bin/systemd-run", unit_name="hermes-updater-123"
        )
        assert prefix.endswith("-- ")
        assert "--scope" in prefix
        assert "hermes-updater-123" in prefix

    def test_shell_prefix_is_empty_without_systemd_run(self):
        assert (
            update_quiesce.isolated_updater_shell_prefix(
                systemd_run=None, unit_name="hermes-updater-123"
            )
            == ""
        )

    def test_unit_name_is_unique_per_updater(self):
        first = update_quiesce.updater_scope_unit_name(pid=11, stamp=1.0)
        second = update_quiesce.updater_scope_unit_name(pid=12, stamp=1.0)
        assert first != second
        assert first.startswith("hermes-updater-")
        # systemd unit names must not carry path separators or spaces.
        assert all(ch.isalnum() or ch in "-_." for ch in first), first
