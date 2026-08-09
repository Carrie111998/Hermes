#!/usr/bin/env python3
"""
Unit tests for the AST Search tool module in hermes-agent (ported from Cortex Agent).
"""

import pytest
from pathlib import Path
from tools.ast_search import ast_search_tool, _parse_file_ast, check_ast_search_requirements
from tools.registry import registry


def test_ast_search_requirements():
    assert check_ast_search_requirements() is True


def test_ast_search_tool_nonexistent_path():
    res = ast_search_tool(path="/non/existent/path/file.py")
    assert "does not exist" in res.lower() or "error" in res.lower()


def test_ast_search_tool_non_python_file(tmp_path):
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Hello World", encoding="utf-8")
    res = ast_search_tool(path=str(txt_file))
    assert "supported on python" in res.lower()


def test_ast_search_tool_single_file(tmp_path):
    code = '''
import os
import json

class Calculator:
    """A simple calculator class."""
    def add(self, a, b):
        return a + b

async def fetch_data(url):
    """Fetch data asynchronously."""
    pass
'''
    py_file = tmp_path / "sample.py"
    py_file.write_text(code, encoding="utf-8")

    # Test all symbols
    res_all = ast_search_tool(path=str(py_file), symbol_type="all")
    assert "Calculator" in res_all
    assert "add" in res_all
    assert "fetch_data" in res_all
    assert "os" in res_all

    # Test class filtering
    res_class = ast_search_tool(path=str(py_file), symbol_type="class")
    assert "Calculator" in res_class
    assert "fetch_data" not in res_class

    # Test query filtering
    res_query = ast_search_tool(path=str(py_file), query="fetch")
    assert "fetch_data" in res_query
    assert "Calculator" not in res_query


def test_ast_search_tool_directory(tmp_path):
    dir_path = tmp_path / "src"
    dir_path.mkdir()
    (dir_path / "mod1.py").write_text("class ModuleOne:\n    pass\n", encoding="utf-8")
    (dir_path / "mod2.py").write_text("def module_two_fn():\n    pass\n", encoding="utf-8")

    res = ast_search_tool(path=str(dir_path))
    assert "ModuleOne" in res
    assert "module_two_fn" in res


def test_ast_search_registered_in_registry():
    entry = registry.get_entry("ast_search")
    assert entry is not None
    assert entry.name == "ast_search"
    assert entry.toolset == "ast_search"
