"""Integration contract for the external Hermes host updater guard."""

from pathlib import Path


UPDATER = Path("/Users/ryanchao/.hermes/scripts/hermes-host-updater.sh")


def test_host_updater_runs_cron_guard_before_declaring_up_to_date():
    source = UPDATER.read_text(encoding="utf-8")

    guard_call = '"$PYTHON" -m cron.routing_guard check'
    assert guard_call in source
    assert source.index(guard_call) < source.index('if [[ "$BEHIND" == "0" ]]')
    assert "cron_routing_guard_failed" in source


def test_host_updater_rechecks_after_merge_and_after_gateway_health_with_source_only_rollback():
    source = UPDATER.read_text(encoding="utf-8")

    # Must still contain four guard check invocations (def + 3 call sites).
    assert source.count("check_cron_routing_guard") >= 4

    # Pre-update check distinguishes rc=2 (bootstrap_required) from rc!=0 (blocked).
    assert "bootstrap_required" in source
    assert "cron_routing_manifest_missing_before_update" in source
    assert "cron_routing_guard_failed_before_update" in source
    assert source.index("bootstrap_required") < source.index('if [[ "$BEHIND" == "0" ]]')

    # Post-merge check still rollback-only (never modifies jobs.json/config.yaml).
    # Use the second "launchctl kickstart" occurrence (the first is inside the
    # rollback function itself).
    first_restart = source.index("launchctl kickstart -k")
    live_restart = source.index("launchctl kickstart -k", first_restart + 1)
    post_merge_region = source[source.index("post_merge_compile_failed"):live_restart]
    assert "check_cron_routing_guard" in post_merge_region
    assert "cron_routing_manifest_missing_post_merge" in post_merge_region
    assert "cron_routing_guard_failed_post_merge" in post_merge_region
    assert "rollback_source_and_restore_gateway" in post_merge_region

    # Post-health-check guard distinguishes bootstrap_required vs guard_failed.
    assert source.index('"$HERMES" gateway status') < source.index("cron_routing_manifest_missing_post_restart")
    assert "cron_routing_guard_failed_post_restart" in source
    assert "merged_upstream_gateway_healthy_and_cron_routing_verified" in source

    assert source.count("rollback_source_and_restore_gateway \"$HEAD\"") == 4
    assert "cron.routing_guard restore" not in source
    assert "gateway_restart_failed_rolled_back" in source
    assert "gateway_healthcheck_timeout_rolled_back" in source

