"""Tests for database CLI commands."""

import argparse
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_cmd_init_sqlite():
    """Test SQLite init command."""
    from hermes_cli.db_commands import cmd_init
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        args = argparse.Namespace(
            backend="sqlite",
            sqlite_path=db_path,
            postgres_url=None,
        )
        
        result = cmd_init(args)
        
        assert result == 0
        assert db_path.exists()


def test_cmd_status_sqlite():
    """Test SQLite status command."""
    from hermes_cli.db_commands import cmd_status
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        # Init first
        args = argparse.Namespace(
            backend="sqlite",
            sqlite_path=db_path,
            postgres_url=None,
        )
        
        from hermes_cli.db_commands import cmd_init
        cmd_init(args)
        
        # Check status
        result = cmd_status(args)
        assert result == 0


def test_cmd_init_missing_postgres_url():
    """Test that init fails without Postgres URL."""
    from hermes_cli.db_commands import cmd_init
    
    args = argparse.Namespace(
        backend="postgres",
        sqlite_path=None,
        postgres_url=None,
    )
    
    with patch.dict("os.environ", {}, clear=True):
        result = cmd_init(args)
        
    assert result == 1


def test_cmd_status_not_initialized():
    """Test status for uninitialized database."""
    from hermes_cli.db_commands import cmd_status
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        args = argparse.Namespace(
            backend="sqlite",
            sqlite_path=db_path,
            postgres_url=None,
        )
        
        result = cmd_status(args)
        
        assert result == 0


def test_backend_detection_sqlite():
    """Test that backend detection defaults to SQLite."""
    from hermes_cli.db_commands import _detect_backend
    
    with patch.dict("os.environ", {}, clear=True):
        backend = _detect_backend()
        
    assert backend == "sqlite"


def test_backend_detection_postgres_with_database_url():
    """Test that backend detects Postgres with DATABASE_URL."""
    from hermes_cli.db_commands import _detect_backend
    
    with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
        backend = _detect_backend()
        
    assert backend == "postgres"


def test_backend_detection_postgres_with_authority_url():
    """Test that backend detects Postgres with AUTHORITY_POSTGRES_URL."""
    from hermes_cli.db_commands import _detect_backend
    
    with patch.dict("os.environ", {"AUTHORITY_POSTGRES_URL": "postgresql://auth"}):
        backend = _detect_backend()
        
    assert backend == "postgres"


def test_add_db_parser():
    """Test that database parser is added correctly."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    
    from hermes_cli.db_commands import add_db_parser
    add_db_parser(subparsers)
    
    # Parse various commands
    args = parser.parse_args(["db", "status"])
    assert args.db_command == "status"
    
    args = parser.parse_args(["db", "init"])
    assert args.db_command == "init"
    
    args = parser.parse_args(["db", "init", "--backend", "postgres"])
    assert args.backend == "postgres"
