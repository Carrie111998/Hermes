from agent.context_compressor import ContextCompressor


def _compressor(policy):
    comp = ContextCompressor("test", quiet_mode=True, config_context_length=100000, protect_first_n=1)
    comp.set_pa_compression_policy(policy)
    return comp


def _messages(count=100):
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "start"}]
    for i in range(count):
        if i in {10, 40, 70, 90, 95}:
            messages.append(
                {
                    "role": "user",
                    "content": f"Worker update: Block 14 Unit {i:02d} completed, photos attached",
                }
            )
        else:
            messages.append({"role": "user", "content": f"chatter {i}"})
    return messages


def test_preserve_case_state_keeps_mutations_and_drops_chatter():
    policy = {
        "strategy": "preserve-case-state",
        "window_size": 8,
        "preserve_fields": ["block", "unit", "worker", "photo"],
    }
    compressed = _compressor(policy).compress(_messages(), current_tokens=90000)
    rendered = "\n".join(str(msg.get("content", "")) for msg in compressed)

    assert "Worker update: Block 14 Unit 10 completed" in rendered
    assert "Worker update: Block 14 Unit 40 completed" in rendered
    assert "Worker update: Block 14 Unit 70 completed" in rendered
    assert "Worker update: Block 14 Unit 90 completed" in rendered
    assert "chatter 1" not in rendered
    assert "chatter 99" in rendered
    assert "preserve-case-state" in rendered


def test_preserve_recent_keeps_recent_window_intact():
    policy = {"strategy": "preserve-recent", "window_size": 20}
    messages = [{"role": "system", "content": "system"}] + [
        {"role": "user", "content": f"mgmt turn {i}"} for i in range(30)
    ]
    compressed = _compressor(policy).compress(messages, current_tokens=90000)
    rendered = [msg["content"] for msg in compressed]

    assert "mgmt turn 0" not in rendered
    assert rendered[-20:] == [f"mgmt turn {i}" for i in range(10, 30)]
    assert any("preserve-recent" in item for item in rendered)


def test_preserve_recent_within_window_keeps_full_recall():
    policy = {"strategy": "preserve-recent", "window_size": 20}
    messages = [{"role": "system", "content": "system"}] + [
        {"role": "user", "content": f"mgmt turn {i}"} for i in range(20)
    ]
    compressed = _compressor(policy).compress(messages, current_tokens=90000)

    assert compressed == messages
