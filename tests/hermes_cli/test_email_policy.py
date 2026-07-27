from hermes_cli.email_policy import evaluate_agentmail_free_capacity


def test_agentmail_stays_free_until_capacity_warrants_procurement():
    assert evaluate_agentmail_free_capacity(
        inboxes=1, emails_month=100, emails_day=5, storage_mb=20, webhook_endpoints=1
    ).status == "within_free_tier"
    decision = evaluate_agentmail_free_capacity(
        inboxes=2, emails_month=2500, emails_day=80, storage_mb=100, webhook_endpoints=1
    )
    assert decision.status == "review_required"
    assert decision.requires_procurement_review


def test_agentmail_limit_does_not_silently_authorize_paid_upgrade():
    decision = evaluate_agentmail_free_capacity(
        inboxes=4, emails_month=100, emails_day=5, storage_mb=20, webhook_endpoints=1
    )
    assert decision.status == "capacity_exceeded"
    assert decision.requires_procurement_review
