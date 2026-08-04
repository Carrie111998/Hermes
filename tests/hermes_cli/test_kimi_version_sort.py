"""Regression tests for 'k'/'K' version prefix parsing in _model_sort_key (#78886).

Ensures models prefixed with 'k' or 'K' (e.g., Moonshot Kimi: kimi-k3, kimi-k2.6,
kimi-k3.5) get their numeric version correctly parsed so higher versions outrank
lower ones when sorting catalog search results.
"""

from hermes_cli.model_switch import _model_sort_key


class TestKimiVersionSort:
    def test_k_version_parsing_keys(self):
        p = "moonshotai/kimi"
        key_k3 = _model_sort_key("moonshotai/kimi-k3", p)
        key_k26 = _model_sort_key("moonshotai/kimi-k2.6", p)
        key_k35 = _model_sort_key("moonshotai/kimi-k3.5", p)

        assert key_k3[0] == -3.0
        assert key_k26[0] == -2.6
        assert key_k35[0] == -3.5

    def test_k_version_ordering(self):
        p = "moonshotai/kimi"

        # kimi-k3 outranks kimi-k2.6
        models1 = ["moonshotai/kimi-k2.6", "moonshotai/kimi-k3"]
        models1.sort(key=lambda m: _model_sort_key(m, p))
        assert models1[0] == "moonshotai/kimi-k3"

        # kimi-k3.5 outranks kimi-k2.6
        models2 = ["moonshotai/kimi-k2.6", "moonshotai/kimi-k3.5"]
        models2.sort(key=lambda m: _model_sort_key(m, p))
        assert models2[0] == "moonshotai/kimi-k3.5"

    def test_capital_k_version_parsing(self):
        p = "kimi"
        key_k3 = _model_sort_key("kimi-K3", p)
        key_k26 = _model_sort_key("kimi-K2.6", p)

        assert key_k3[0] == -3.0
        assert key_k26[0] == -2.6

    def test_existing_version_prefixes_unchanged(self):
        assert _model_sort_key("mimo-v2.5-pro", "mimo") == (-2.5, 0, "pro")
        assert _model_sort_key("gpt-5.6-sol", "gpt") == (-5.6, 0, "sol")
        assert _model_sort_key("claude-v4.5-opus", "claude") == (-4.5, 1, "opus")
