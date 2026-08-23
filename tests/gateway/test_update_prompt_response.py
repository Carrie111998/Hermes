import json

from gateway.update_prompt_response import write_update_confirmation_response


def _markers(home, *, prompt_id="prompt-new", correlation_id="corr-new"):
    (home / ".update_pending.json").write_text(
        json.dumps({
            "correlation_id": correlation_id,
            "session_key": "session-new",
            "origin_profile": "work",
            "profile_home": "/profiles/work",
            "control_home": str(home),
            "install_root": "/project/hermes",
            "install_id": "install-1",
        })
    )
    (home / ".update_prompt.json").write_text(
        json.dumps({
            "id": prompt_id,
            "kind": "update_confirmation",
            "correlation_id": correlation_id,
            "context": {
                "origin_profile": "work",
                "profile_home": "/profiles/work",
                "control_home": str(home),
                "install_root": "/project/hermes",
                "install_id": "install-1",
            },
        })
    )


def test_stale_prompt_and_cross_session_callbacks_are_inert(tmp_path):
    _markers(tmp_path)

    assert not write_update_confirmation_response(
        tmp_path,
        prompt_id="prompt-old",
        correlation_id="corr-old",
        session_key="session-new",
        answer="yes",
    )
    assert not write_update_confirmation_response(
        tmp_path,
        prompt_id="prompt-new",
        correlation_id="corr-new",
        session_key="session-other",
        answer="yes",
    )
    assert not (tmp_path / ".update_response").exists()


def test_missing_origin_identity_fails_closed(tmp_path):
    _markers(tmp_path)
    pending_path = tmp_path / ".update_pending.json"
    pending = json.loads(pending_path.read_text())
    pending.pop("control_home")
    pending_path.write_text(json.dumps(pending))

    assert not write_update_confirmation_response(
        tmp_path,
        prompt_id="prompt-new",
        correlation_id="corr-new",
        session_key="session-new",
        answer="yes",
    )
    assert not (tmp_path / ".update_response").exists()


def test_duplicate_callback_cannot_overwrite_first_answer(tmp_path):
    _markers(tmp_path)

    assert write_update_confirmation_response(
        tmp_path,
        prompt_id="prompt-new",
        correlation_id="corr-new",
        session_key="session-new",
        answer="yes",
    )
    assert not write_update_confirmation_response(
        tmp_path,
        prompt_id="prompt-new",
        correlation_id="corr-new",
        session_key="session-new",
        answer="no",
    )
    assert json.loads((tmp_path / ".update_response").read_text())["answer"] == "yes"
