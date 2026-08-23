"""Regression test for #92554: `hermes plugins enable/disable` destroyed every
user comment in config.yaml (and reinjected the default boilerplate) because
_save_enabled_set/_save_disabled_set re-serialized the whole document through
save_config() instead of writing just the changed key.
"""

import os
from unittest.mock import patch


CONFIG_WITH_COMMENTS = """\
# TOP COMMENT — must survive
model:
  provider: test
plugins:
  # rationale for the enabled list
  enabled: []
"""


class TestPluginEnableDisablePreserveComments:
    def test_save_enabled_set_preserves_user_comments(self, tmp_path):
        from hermes_cli.plugins_cmd import _save_enabled_set

        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _save_enabled_set({"demo"})

        written = config_path.read_text(encoding="utf-8")
        assert "# TOP COMMENT — must survive" in written
        assert "# rationale for the enabled list" in written
        assert "demo" in written
        # No boilerplate reinjection from the full-document writer.
        assert "Fallback Model" not in written

    def test_save_disabled_set_preserves_user_comments(self, tmp_path):
        from hermes_cli.plugins_cmd import _save_disabled_set

        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _save_disabled_set({"demo"})

        written = config_path.read_text(encoding="utf-8")
        assert "# TOP COMMENT — must survive" in written
        assert "# rationale for the enabled list" in written
        assert "demo" in written
