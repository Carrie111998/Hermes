from decimal import Decimal


from sigil.order_execution import (
    LiveLaunchControlPolicy,
    LiveLaunchControlRequest,
    LiveLaunchControlStatus,
    LiveTradingCertification,
    LiveTradingCertificationStatus,
    arm_live_launch_control,
    effective_live_launch_control_status,
    suspend_live_launch_control,
)


def _certification(
    status: LiveTradingCertificationStatus = (
        LiveTradingCertificationStatus.CERTIFIED
    ),
) -> LiveTradingCertification:
    return LiveTradingCertification(
        certification_id="certification-1",
        eligibility_review_id="review-1",
        request_id="cert-request-1",
        certifier_identity="owner-1",
        status=status,
        broker_name="public",
        account_identifier="account-1",
        asset_classes=("equity",),
        order_types=("limit",),
        symbols=("AAPL", "MSFT"),
        initial_capital=Decimal("25"),
        maximum_order_notional=Decimal("5"),
        valid_from_epoch=1000,
        valid_until_epoch=2000,
        kill_switch_reference="kill-switch-1",
        rollback_plan_reference="rollback-1",
        evidence_references=("certification-evidence",),
        failed_checks=(),
    )


def _request(**overrides) -> LiveLaunchControlRequest:
    values = dict(
        request_id="launch-request-1",
        broker_name="public",
        account_identifier="account-1",
        asset_classes=("equity",),
        order_types=("limit",),
        symbols=("AAPL",),
        live_capital=Decimal("25"),
        maximum_order_notional=Decimal("5"),
        maximum_daily_loss=Decimal("5"),
        maximum_open_positions=1,
        launch_from_epoch=1100,
        launch_until_epoch=1200,
        operator_identity="owner-1",
        operator_approved=True,
        kill_switch_armed=True,
        rollback_confirmed=True,
        authorization_reference="launch-token-1",
        evidence_references=("launch-evidence",),
    )
    values.update(overrides)
    return LiveLaunchControlRequest(**values)


def test_arms_when_every_launch_check_passes() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(),
        evaluated_at_epoch=1150,
    )

    assert control.status is LiveLaunchControlStatus.ARMED
    assert not control.failed_checks
    assert control.symbols == ("AAPL",)


def test_blocks_inactive_certification() -> None:
    control = arm_live_launch_control(
        _certification(LiveTradingCertificationStatus.REVOKED),
        _request(),
        evaluated_at_epoch=1150,
    )

    assert control.status is LiveLaunchControlStatus.BLOCKED
    assert "certification_active" in control.failed_checks


def test_blocks_symbol_outside_certification_scope() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(symbols=("TSLA",)),
        evaluated_at_epoch=1150,
    )

    assert control.status is LiveLaunchControlStatus.BLOCKED
    assert "symbol_scope_match" in control.failed_checks


def test_blocks_missing_operator_approval() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(operator_approved=False),
        evaluated_at_epoch=1150,
    )

    assert control.status is LiveLaunchControlStatus.BLOCKED
    assert "operator_approval" in control.failed_checks


def test_blocks_daily_loss_over_policy() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(maximum_daily_loss=Decimal("6")),
        evaluated_at_epoch=1150,
        policy=LiveLaunchControlPolicy(maximum_daily_loss=Decimal("5")),
    )

    assert control.status is LiveLaunchControlStatus.BLOCKED
    assert "daily_loss_limit" in control.failed_checks


def test_armed_control_expires_after_launch_window() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(),
        evaluated_at_epoch=1150,
    )

    assert (
        effective_live_launch_control_status(control, at_epoch=1200)
        is LiveLaunchControlStatus.EXPIRED
    )


def test_armed_control_can_be_suspended() -> None:
    control = arm_live_launch_control(
        _certification(),
        _request(),
        evaluated_at_epoch=1150,
    )
    suspended = suspend_live_launch_control(
        control,
        suspended_at_epoch=1175,
        reason="operator emergency stop",
    )

    assert suspended.status is LiveLaunchControlStatus.SUSPENDED
    assert suspended.suspension_reason == "operator emergency stop"


def test_launch_control_id_is_deterministic() -> None:
    kwargs = dict(
        certification=_certification(),
        request=_request(),
        evaluated_at_epoch=1150,
    )
    first = arm_live_launch_control(**kwargs)
    second = arm_live_launch_control(**kwargs)

    assert first.launch_control_id == second.launch_control_id
