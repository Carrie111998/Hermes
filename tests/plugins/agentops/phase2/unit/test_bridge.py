from plugins.agentops.bridge import BoundedBridgeBuffer


def test_failed_bridge_delivery_is_bounded_and_does_not_escape_to_caller(make_event):
    bridge = BoundedBridgeBuffer(capacity=2)

    def closed_consumer(event):
        raise ConnectionError("closed")

    first = bridge.publish(make_event("evt-0001"), closed_consumer)
    bridge.publish(make_event("evt-0002"), closed_consumer)
    third = bridge.publish(make_event("evt-0003"), closed_consumer)

    assert first.delivered is False and first.queued is True
    assert bridge.depth == 2
    assert third.dropped == 1
    delivered = []
    assert bridge.drain(delivered.append) == 2
    assert [event.event_id for event in delivered] == ["evt-0002", "evt-0003"]
