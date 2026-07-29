#!/usr/bin/env python3
import sys
import os

# Add the current directory to sys.path and import the core module directly
# Use exec to load the module content
with open('/c/Users/downl/Documents/New project/hermes-agent/plugins/lm-twitterer/core.py', 'r', encoding='utf-8') as f:
    core_content = f.read()

# First, let's check if reply_mentions function exists
if 'def reply_mentions(' not in core_content:
    print("ERROR: reply_mentions function not found in core.py")
    sys.exit(1)

# Define required globals that core.py expects
import types
mock_module = types.ModuleType('mock_module')
sys.modules['hermes_constants'] = mock_module
mock_module.get_hermes_home = lambda: '/c/Users/downl/.hermes'

# Mock hermes_cli.config.get_env_value
def mock_get_env_value(name):
    # Read from the .env file
    env_path = '/c/Users/downl/.hermes/.env'
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if line.startswith(name + '='):
                    value = line.split('=', 1)[1].strip()
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    return value
    except Exception:
        pass
    return None

# Mock the module
class MockHermesCLlConfig:
    get_env_value = staticmethod(mock_get_env_value)

sys.modules['hermes_cli.config'] = MockHermesCLlConfig()

# Now let's create a custom namespace and execute the module
test_globals = {
    '__name__': '__main__',
    '__file__': '/c/Users/downl/Documents/New project/hermes-agent/plugins/lm-twitterer/core.py',
    '__package__': 'lm_twitterer',
}

# Create our module namespace
module_globals = {}

# We need to execute the code to get to the reply_mentions function
# Let's read the core.py file and manually extract what we need
with open('/c/Users/downl/.hermes/.env', 'r') as env_file:
    env_content = env_file.read()

# Parse LM_TWITTERER_ENV values from env file
lm_twitterer_env_vars = {}
for line in env_content.splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        if key.startswith('LM_TWITTERER_'):
            # Remove quotes
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            lm_twitterer_env_vars[key] = value

# Set up the environment
os.environ.update(lm_twitterer_env_vars)

# Set up the required globals
exec('from pathlib import Path', module_globals)
exec('from dataclasses import dataclass', module_globals)
exec('from typing import Any, Callable, Iterable', module_globals)
exec('import json, os, re, sqlite3, time, urllib.error, urllib.parse, urllib.request', module_globals)
exec('import sys', module_globals)
module_globals['get_hermes_home'] = lambda: '/c/Users/downl/.hermes'
module_globals['get_env_value'] = mock_get_env_value
module_globals['sys'] = sys
module_globals['os'] = os

# Execute the core.py content
exec(core_content, module_globals)

# Now we should have the reply_mentions function
if 'reply_mentions' not in module_globals:
    print("ERROR: reply_mentions function not found after execution")
    sys.exit(1)
    
if '_json' not in module_globals:
    print("ERROR: _json function not found after execution")
    sys.exit(1)

# Call the function
result = module_globals['reply_mentions'](
    dry_run=False,
    count=50,
    mark_seen_on_dry_run=False,
    provider='moa',
    model='hakuapulse-orchestrator'
)

# Format the result as JSON
print(module_globals['_json'](result))