"""String-typed toolset lists must be coerced, not iterated per character.

A YAML-quoted string (``disabled_toolsets: '["web", "browser"]'``) or bare
scalar (``disabled_toolsets: web``) in config.yaml reaches consumers as a
Python string. Iterating it yields characters, matches no toolset, and the
denylist is silently ignored — the full tool schema set is then sent on every
model call (#61264, #61265).
"""

import logging

import model_tools
from cron.scheduler import _resolve_cron_disabled_toolsets
from toolsets import normalize_toolset_spec


class TestNormalizeToolsetSpec:
    def test_none_passthrough(self):
        assert normalize_toolset_spec(None) is None

    def test_list_passthrough(self):
        assert normalize_toolset_spec(["web", "browser"]) == ["web", "browser"]

    def test_tuple_becomes_list(self):
        assert normalize_toolset_spec(("web",)) == ["web"]

    def test_json_string_decodes(self, caplog):
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            assert normalize_toolset_spec('["web", "browser"]') == ["web", "browser"]
        assert "configured as the string" in caplog.text

    def test_bare_scalar_string(self):
        assert normalize_toolset_spec("web") == ["web"]

    def test_comma_separated_string(self):
        assert normalize_toolset_spec("web, browser") == ["web", "browser"]

    def test_malformed_json_falls_back_to_comma_split(self):
        # Unbalanced bracket: json.loads fails; names are still recovered.
        assert normalize_toolset_spec('["web", "browser"') == ["web", "browser"]

    def test_quoted_names_in_comma_form(self):
        assert normalize_toolset_spec("'web', \"browser\"") == ["web", "browser"]


class TestModelToolsStringCoercion:
    def test_string_disabled_toolsets_equals_list_form(self):
        as_list = {
            t["function"]["name"]
            for t in model_tools.get_tool_definitions(
                disabled_toolsets=["terminal", "file"], quiet_mode=True
            )
        }
        as_yaml_string = {
            t["function"]["name"]
            for t in model_tools.get_tool_definitions(
                disabled_toolsets='["terminal", "file"]', quiet_mode=True
            )
        }
        assert as_yaml_string == as_list
        # And the disable actually took: unfiltered catalog is strictly larger.
        everything = {
            t["function"]["name"]
            for t in model_tools.get_tool_definitions(quiet_mode=True)
        }
        assert as_yaml_string < everything

    def test_string_enabled_toolsets_equals_list_form(self):
        as_list = {
            t["function"]["name"]
            for t in model_tools.get_tool_definitions(
                enabled_toolsets=["terminal"], quiet_mode=True
            )
        }
        as_string = {
            t["function"]["name"]
            for t in model_tools.get_tool_definitions(
                enabled_toolsets="terminal", quiet_mode=True
            )
        }
        assert as_string == as_list


class TestCronDisabledToolsetsStringCoercion:
    def test_string_config_yields_names_not_characters(self):
        cfg = {"agent": {"disabled_toolsets": '["web", "browser"]'}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "web" in disabled
        assert "browser" in disabled
        # No single-character garbage from per-char iteration.
        assert not any(len(name) == 1 for name in disabled)

    def test_list_config_unchanged(self):
        cfg = {"agent": {"disabled_toolsets": ["web"]}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert disabled == ["cronjob", "messaging", "clarify", "web"]


class TestPlatformToolsDisabledToolsetsStringCoercion:
    """The global denylist must hold for the shape `hermes config set` writes.

    ``hermes config set agent.disabled_toolsets web`` stores the bare scalar
    ``web``: the setter only auto-coerces bool/int/float, so a name stays a
    string. ``_get_platform_tools`` then subtracted ``{'w','e','b'}``, which
    matches no toolset, and the disabled toolset stayed enabled — the same
    denylist bypass #25752 closed, reached through the CLI rather than a
    per-job override. This resolver feeds gateway setup and prompt-size.
    """

    BASE = {"platform_toolsets": {"cli": ["web", "file"]}}

    def _resolve(self, disabled):
        from hermes_cli.tools_config import _get_platform_tools

        cfg = dict(self.BASE, agent={"disabled_toolsets": disabled})
        return _get_platform_tools(cfg, "cli")

    def test_bare_string_disables_the_toolset(self):
        """The `hermes config set` shape — regression for the bypass."""
        assert "web" not in self._resolve("web")

    def test_json_string_disables_the_toolset(self):
        assert "web" not in self._resolve('["web"]')

    def test_string_and_list_agree(self):
        """String input must resolve identically to the equivalent list."""
        assert self._resolve("web") == self._resolve(["web"])

    def test_list_config_unchanged(self):
        """The documented shape keeps working — no behaviour change for it."""
        resolved = self._resolve(["web"])
        assert "web" not in resolved
        assert "file" in resolved

    def test_absent_key_leaves_toolsets_enabled(self):
        from hermes_cli.tools_config import _get_platform_tools

        resolved = _get_platform_tools(dict(self.BASE), "cli")
        assert {"web", "file"} <= resolved
