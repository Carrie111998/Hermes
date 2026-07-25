from decimal import Decimal

from sigil.order_execution import (
    LiveLaunchControl,
    LiveLaunchControlStatus,
    LiveOrderAdmissionPolicy,
    LiveOrderAdmissionRequest,
    LiveOrderAdmissionStatus,
    evaluate_live_order_admission,
)


def _control(
    status: LiveLaunchControlStatus = LiveLaunchControlStatus.ARMED,
) -> LiveLaunchControl:
    return LiveLaunchControl(
        launch_control_id="launch-control-1",
        certification_id="certification-1",
        request_id="launch-request-1",
        status=status,
        broker_name="public",
        account_identifier="account-1",
        asset_classes=("equity",),
        order_types=("limit",),
        symbols=("AAPL", "MSFT"),
        live_capital=Decimal("25"),
        maximum_order_notional=Decimal("5"),
        maximum_daily_loss=Decimal("5"),
        maximum_open_positions=1,
        launch_from_epoch=1000,
        launch_until_epoch=2000,
        operator_identity="owner-1",
        authorization_reference="launch-token-1",
        evidence_references=("launch-evidence",),
        passed_checks=("launch_control_armed",),
        failed_checks=(),
    )


def _request(**overrides) -> LiveOrderAdmissionRequest:
    values = dict(
        request_id="order-request-1",
        client_order_id="client-order-1",
        broker_name="public",
        account_identifier="account-1",
        asset_class="equity",
        symbol="AAPL",
        order_type="limit",
        side="buy",
        quantity=Decimal("1"),
        limit_price=Decimal("5"),
        estimated_notional=Decimal("5"),
        committed_live_capital=Decimal("0"),
        realized_daily_loss=Decimal("0"),
        open_position_count=0,
        market_data_observed_at_epoch=1490,
        operator_authorization_reference="launch-token-1",
        evidence_references=("order-evidence",),
    )
    values.update(overrides)
    return LiveOrderAdmissionRequest(**values)


def test_admits_valid_order() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(),
        evaluated_at_epoch=1500,
    )

    assert admission.status is LiveOrderAdmissionStatus.ADMITTED
    assert not admission.failed_checks
    assert admission.projected_live_capital == Decimal("5")


def test_rejects_order_over_notional_limit() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(estimated_notional=Decimal("5.01")),
        evaluated_at_epoch=1500,
    )

    assert admission.status is LiveOrderAdmissionStatus.REJECTED
    assert "order_notional_within_limit" in admission.failed_checks


def test_rejects_capital_over_launch_ceiling() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(committed_live_capital=Decimal("22")),
        evaluated_at_epoch=1500,
    )

    assert admission.status is LiveOrderAdmissionStatus.REJECTED
    assert "live_capital_within_limit" in admission.failed_checks


def test_rejects_stale_market_data() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(market_data_observed_at_epoch=1400),
        evaluated_at_epoch=1500,
        policy=LiveOrderAdmissionPolicy(
            maximum_market_data_age_seconds=30
        ),
    )

    assert admission.status is LiveOrderAdmissionStatus.REJECTED
    assert "market_data_fresh" in admission.failed_checks


def test_marks_duplicate_client_order_id() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(),
        evaluated_at_epoch=1500,
        prior_client_order_ids=("client-order-1",),
    )

    assert admission.status is LiveOrderAdmissionStatus.DUPLICATE
    assert "not_duplicate" in admission.failed_checks


def test_marks_expired_launch_control() -> None:
    admission = evaluate_live_order_admission(
        _control(),
        _request(market_data_observed_at_epoch=2000),
        evaluated_at_epoch=2000,
    )

    assert admission.status is LiveOrderAdmissionStatus.EXPIRED
    assert "launch_control_armed" in admission.failed_checks


def test_marks_suspended_launch_control() -> None:
    admission = evaluate_live_order_admission(
        _control(LiveLaunchControlStatus.SUSPENDED),
        _request(),
        evaluated_at_epoch=1500,
    )

    assert admission.status is LiveOrderAdmissionStatus.SUSPENDED
    assert "launch_control_armed" in admission.failed_checks


def test_admission_id_is_deterministic() -> None:
    kwargs = dict(
        launch_control=_control(),
        request=_request(),
        evaluated_at_epoch=1500,
    )
    first = evaluate_live_order_admission(**kwargs)
    second = evaluate_live_order_admission(**kwargs)

    assert first.admission_id == second.admission_id
