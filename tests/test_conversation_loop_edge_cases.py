import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_agent():
    """Ajan istemcisinin tip, string ve sayısal durum hatalarını önleyen eksiksiz mock yapısı."""
    agent = MagicMock()
    # Düz dict dönen sync/mock yanıt
    agent.run.return_value = {"status": "success", "response": "OK"}
    
    # Sayısal ve mantıksal varsayılan değerler
    agent._memory_nudge_interval = 0
    agent._user_turn_count = 0
    agent._turns_since_memory = 0
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.max_iterations = 10
    agent.compression_enabled = False
    agent.compression_idle_compact_after_seconds = 0
    agent.run_budget_seconds = None
    
    # Token & Cost sayaçları
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "ok"
    agent.session_cost_source = "test"
    
    # String dönmesi gereken metot ve nitelikler
    agent._strip_think_blocks = lambda x: x if isinstance(x, str) else ""
    agent._drain_pending_redirect = MagicMock(return_value=None)
    agent._drain_pending_steer = MagicMock(return_value=None)
    agent._memory_manager = MagicMock()
    agent._memory_manager.prefetch_all = MagicMock(return_value="")
    agent._current_streamed_assistant_text = ""
    agent._cached_system_prompt = "Test System Prompt"
    agent._execution_thread_id = "test_thread"
    agent.session_id = "test_session"
    agent.model = "test_model"
    agent.provider = "test_provider"
    agent.platform = "test_platform"
    agent.api_mode = "default"
    agent.base_url = "https://api.test.com"
    
    # Helper & Gate metotları
    agent._file_mutation_verifier_enabled = MagicMock(return_value=False)
    agent._turn_completion_explainer_enabled = MagicMock(return_value=False)
    agent._tool_guardrail_halt_decision = None
    agent._interrupt_message = None
    agent.iteration_budget = None
    agent.context_compressor = None
    
    # Nesne ve veri yapıları
    agent.valid_tool_names = set()
    agent._memory_store = None
    agent._todo_store = MagicMock()
    agent._todo_store.has_items.return_value = True
    
    # State flags & callbacks
    agent._relay_pending_turn_id = None
    agent._inflight_turn_id = None
    agent._compression_warning = None
    agent.quiet_mode = True
    agent.reaction_callback = None
    
    return agent


def test_run_conversation_invalid_moa_config_handling(mock_agent):
    """moa_config parametresi hatalı/boş verildiğinde güvenli çökme kontrolü."""
    invalid_configs = [{}, {"providers": []}, None]

    with patch("agent.conversation_loop._ra") as mock_ra:
        mock_ra.return_value._set_interrupt = MagicMock()
        
        from agent.conversation_loop import run_conversation
        for config in invalid_configs:
            try:
                result = run_conversation(
                    agent=mock_agent,
                    user_message="Test message",
                    moa_config=config
                )
                assert result is not None
            except Exception as e:
                pytest.fail(f"run_conversation moa_config={config} ile patladı: {e}")


def test_run_conversation_stream_callback_none_type(mock_agent):
    """stream_callback None geçildiğinde varsayılan senaryonun çalıştığını teyit eder."""
    with patch("agent.conversation_loop._ra") as mock_ra:
        mock_ra.return_value._set_interrupt = MagicMock()
        
        from agent.conversation_loop import run_conversation
        result = run_conversation(
            agent=mock_agent,
            user_message="Hello",
            stream_callback=None
        )
        assert result is not None


def test_run_conversation_type_hints_importability():
    """Fonksiyonun dışarıya aktarılabilirliğini (export/import) doğrular."""
    from agent.conversation_loop import __all__
    assert "run_conversation" in __all__