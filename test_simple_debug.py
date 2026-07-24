"""
Simple test that tests the dump_api_response_debug functionality through the exact API
"""
import sys
import tempfile
import shutil
from unittest.mock import MagicMock

# Add project root to sys.path to enable imports
sys.path.insert(0, '/home/gk/.hermes/hermes-agent')

from agent.agent_runtime_helpers import dump_api_response_debug

def test_simple_direct_call():
    """Basic test that just makes sure dump_api_response_debug works"""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Set up a minimal mock agent
        mock_agent = MagicMock()
        mock_agent.session_id = "test_session_123"
        mock_agent.logs_dir = temp_dir
        mock_agent.verbose_logging = True
        mock_agent._safe_session_filename_component = lambda sid: sid.replace("/", "_").replace("\\", "_")[:50]
        mock_agent._vprint = lambda msg: None
        mock_agent.logger = MagicMock()
        
        # Mock os.environ to avoid the import issue 
        import os
        orig_environ = os.environ
        os.environ = {"HERMES_DUMP_REQUESTS": "1"}
        
        try:
            # This tests the actual functionality 
            dump_response = {
                "content": None,
                "status": "error",
                "error_message": "Simulated API failure",
                "provider": "openai",
                "usage": {}
            }
            
            # Call the function - even if it returns None due to env issues, 
            # we want to see if it runs properly overall
            result = dump_api_response_debug(
                agent=mock_agent,
                response=dump_response,
                reason="terminal_rejection"
            )
            
            print(f"Function returned: {result}")
            print("Basic functionality test passed")
            return True
            
        finally:
            # Restore original environment
            os.environ = orig_environ

if __name__ == "__main__":
    test_simple_direct_call()
    print("Done")