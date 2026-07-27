import sqlite3
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cli import agent_identity


def test_signed_machine_request_is_verified_and_replay_is_rejected():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    agent_identity.register_public_identity(
        conn, organization_id="org_1", agent_id="buyer_1", public_key=public
    )
    issued_at = int(time.time())
    payload = {"invoice_id": "inv_1", "amount_minor": 100}
    message = agent_identity.canonical_request(
        organization_id="org_1",
        agent_id="buyer_1",
        nonce="nonce-123",
        issued_at=issued_at,
        payload=payload,
    )
    signature = private.sign(message)
    kwargs = dict(
        organization_id="org_1",
        agent_id="buyer_1",
        nonce="nonce-123",
        issued_at=issued_at,
        payload=payload,
        signature=signature,
    )
    agent_identity.verify_machine_request(conn, **kwargs)
    with pytest.raises(agent_identity.AgentIdentityError, match="already been used"):
        agent_identity.verify_machine_request(conn, **kwargs)
