import pytest
from agent.result_normalizer import normalize_model_result

@pytest.mark.parametrize("payload,expected", [({"final_response":"deepseek"},"deepseek"), ({"message":{"content":"gpt-oss-20b"}},"gpt-oss-20b"), ({"choices":[{"message":{"content":"gpt-oss-120b"}}]},"gpt-oss-120b")])
def test_text_contract(payload, expected): assert normalize_model_result(payload)["final_response"] == expected
def test_empty_typed_failure(): assert normalize_model_result({})["error"].startswith("unusable_model_result:")
def test_tool_call_contract(): assert normalize_model_result({"tool_calls":[{}]})["failed"] is False
def test_provider_error_contract(): assert normalize_model_result({"error":"down"})["error"] == "unusable_model_result: down"
