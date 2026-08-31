\"\"\"Tests for hermes_cli/logging.py — logging setup helpers.\"\"\"
def test_logging_import():
    from hermes_cli.logging import setup_logging
    assert callable(setup_logging)
