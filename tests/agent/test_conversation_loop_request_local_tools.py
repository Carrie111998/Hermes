from copy import deepcopy

from agent.conversation_loop import _request_local_llama_cpp_tools


def test_llama_cpp_grammar_recovery_keeps_canonical_tools_unchanged():
    canonical = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "format": "email",
                            "pattern": r"^\S+@\S+$",
                        }
                    },
                },
            },
        }
    ]
    before = deepcopy(canonical)

    request_tools, stripped = _request_local_llama_cpp_tools(canonical)

    assert stripped == 2
    assert canonical == before
    assert request_tools is not canonical
    email_schema = request_tools[0]["function"]["parameters"]["properties"]["email"]
    assert "format" not in email_schema
    assert "pattern" not in email_schema
