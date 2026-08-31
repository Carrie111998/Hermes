# Model Provider Plugins

Each subdirectory is a self-contained provider profile plugin. The
directory layout mirrors `plugins/platforms/`:

```
plugins/model-providers/
├── openrouter/
│   ├── __init__.py      # registers the ProviderProfile
│   └── plugin.yaml      # manifest: name, kind, version, description
├── anthropic/
│   ├── __init__.py
│   └── plugin.yaml
└── ...
```

## How discovery works

`providers/__init__.py._discover_providers()` scans this directory (and
`$HERMES_HOME/plugins/model-providers/`) the first time anything calls
`get_provider_profile()` or `list_providers()`. Each `__init__.py` is
imported and expected to call `providers.register_provider(profile)`.

User plugins at `$HERMES_HOME/plugins/model-providers/<name>/` override
bundled plugins of the same name — last-writer-wins in
`register_provider()`. Drop a file there to replace a built-in.

## Adding a new provider

1. Create `plugins/model-providers/<your_provider>/__init__.py`:

   ```python
   from providers import register_provider
   from providers.base import ProviderProfile

   my_provider = ProviderProfile(
       name="your-provider",
       aliases=("alias1", "alias2"),
       display_name="Your Provider",
       description="One-line description shown in the setup picker",
       signup_url="https://your-provider.example.com/keys",
       env_vars=("YOUR_PROVIDER_API_KEY", "YOUR_PROVIDER_BASE_URL"),
       base_url="https://api.your-provider.example.com/v1",
       default_aux_model="your-cheap-model",
   )

   register_provider(my_provider)
   ```

2. Create `plugins/model-providers/<your_provider>/plugin.yaml`:

   ```yaml
   name: your-provider-profile
   kind: model-provider
   version: 1.0.0
   description: Short sentence about the provider
   author: Your Name
   ```

Nothing else needs to change. `auth.py`, `config.py`, `models.py`,
`doctor.py`, `model_metadata.py`, `runtime_provider.py`, and the
chat_completions transport all auto-wire from the registry.

## Adding plan-usage support

A provider that publishes its own quota/limit endpoint can report it to the
desktop "Plan Usage" panel and the `account.usage` RPC by overriding one hook.
Providers that do not are unaffected: the base implementation returns `None`,
which means "nothing to report" and is the normal answer for most of the
registry.

```python
class YourProfile(ProviderProfile):
    def fetch_usage(self, *, credential=None, base_url=None, timeout=8.0):
        import httpx

        from agent.provider_usage_types import (
            UNIT_COUNT, ProviderUsage, UsageWindow, to_decimal,
        )

        token = str(getattr(credential, "access_token", "") or "").strip()
        if not token:
            return None

        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                "https://api.your-provider.example.com/v1/quota",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json() or {}

        return ProviderUsage(
            provider="your-provider",
            display_name="Your Provider",
            plan=payload.get("plan"),
            windows=(
                UsageWindow(
                    label="5h",                       # UI translates known labels
                    unit=UNIT_COUNT,                  # percent | currency | count | tokens
                    limit=to_decimal(payload.get("limit")),
                    remaining=to_decimal(payload.get("remaining")),
                ),
            ),
        )
```

Set `usage_ttl=<seconds>` on the profile to match how fast the provider's own
numbers move — a rolling five-hour window does not need minute polling; a
credit balance moves with every request.

Four rules, each of which exists because breaking it produced a real bug:

1. **Report the provider's own numbers.** Fill `used` / `limit` / `remaining`
   verbatim and let `UsageWindow.used_percent` derive a percentage only when
   the arithmetic is unambiguous. Do not compute one yourself: at least one
   provider ships a field named `used` whose value tracks what is *left*.
2. **Declare the unit.** Dollars, counts and tokens are not percentages, and
   the panel renders each differently.
3. **Never call `load_pool()`.** The aggregator resolves the credential once
   and hands it to you. Seeding is not side-effect free — the Copilot branch
   exchanges a raw `gh` token — so a plugin that re-resolves turns opening a
   panel into a write.
4. **Raise on failure.** The aggregator classifies transport/HTTP errors into
   a typed state that the UI can translate. A message you invent cannot be.

Distribution is the same as any other provider plugin: drop the directory in
`$HERMES_HOME/plugins/model-providers/<name>/` to override a bundled profile,
or ship it as a package with a provider entry point. Neither needs a change to
Hermes itself.

## Non-trivial profiles

Override the `ProviderProfile` hooks in a subclass for per-provider
quirks — see `plugins/model-providers/openrouter/__init__.py` for
`build_extra_body` and `build_api_kwargs_extras` examples, and
`plugins/model-providers/gemini/__init__.py` for `thinking_config`
translation.
