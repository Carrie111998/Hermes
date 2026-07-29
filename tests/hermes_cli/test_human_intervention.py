from hermes_cli.human_intervention import (
    HumanInterventionProvider,
    HumanInterventionRequest,
    HumanInterventionSignal,
    begin_human_intervention,
    finish_human_intervention,
    take_remote_signal,
)


class StubProvider(HumanInterventionProvider):
    def __init__(self, signal=None, *, raise_begin=False, raise_poll=False, raise_finish=False):
        self.signal = signal
        self.raise_begin = raise_begin
        self.raise_poll = raise_poll
        self.raise_finish = raise_finish
        self.finished = []

    def begin(self, request):
        if self.raise_begin:
            raise RuntimeError("begin failure")
        return "handle"

    def poll(self, handle):
        if self.raise_poll:
            raise RuntimeError("poll failure")
        assert handle == "handle"
        return self.signal

    def finish(self, handle, outcome):
        if self.raise_finish:
            raise RuntimeError("finish failure")
        self.finished.append((handle, outcome))


def _request(kind="approval", risk_level="medium", actions=frozenset({"deny", "extend", "approve_once"})):
    return HumanInterventionRequest(
        kind=kind,
        session_key="test",
        title="Test",
        preview="safe preview",
        timeout_seconds=30,
        risk_level=risk_level,
        allowed_actions=actions,
    )


def test_begin_and_finish_are_best_effort():
    provider = StubProvider()
    handle = begin_human_intervention(provider, _request())
    assert handle == "handle"
    finish_human_intervention(provider, handle, "local_response")
    assert provider.finished == [("handle", "local_response")]


def test_provider_failures_do_not_affect_local_wait():
    assert begin_human_intervention(StubProvider(raise_begin=True), _request()) is None
    assert take_remote_signal(StubProvider(raise_poll=True), "handle", _request()) is None
    finish_human_intervention(StubProvider(raise_finish=True), "handle", "timeout")


def test_extend_requires_positive_duration_and_explicit_capability():
    request = _request(actions=frozenset({"extend"}))
    assert take_remote_signal(
        StubProvider(HumanInterventionSignal("extend", extend_seconds=0)), "handle", request
    ) is None
    signal = take_remote_signal(
        StubProvider(HumanInterventionSignal("extend", extend_seconds=60)), "handle", request
    )
    assert signal is not None and signal.extend_seconds == 60


def test_critical_remote_approve_is_a_core_rejection():
    request = _request(risk_level="critical")
    assert take_remote_signal(
        StubProvider(HumanInterventionSignal("approve_once")), "handle", request
    ) is None


def test_only_approval_kind_can_accept_remote_approve_once():
    for kind in ("sudo", "clarify", "computer_use"):
        assert take_remote_signal(
            StubProvider(HumanInterventionSignal("approve_once")), "handle", _request(kind=kind)
        ) is None


def test_signal_must_be_in_core_allowed_actions():
    request = _request(actions=frozenset({"deny"}))
    assert take_remote_signal(
        StubProvider(HumanInterventionSignal("approve_once")), "handle", request
    ) is None
