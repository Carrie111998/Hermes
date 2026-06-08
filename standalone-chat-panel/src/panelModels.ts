export interface PanelModelInfo {
  provider?: string;
  model?: string;
}

interface PanelModelProvider {
  name: string;
  slug: string;
  models: string[];
  warning: string;
}

interface PanelModelOptionsResponse {
  model?: string;
  provider?: string;
  providers?: unknown[];
}

interface PanelConfigSetResponse {
  value?: string;
  warning?: string;
}

interface PanelModelChoice {
  value: string;
  label: string;
  provider: string;
  model: string;
}

interface PanelModelControllerOptions {
  select: HTMLSelectElement;
  status: HTMLElement;
  request<T>(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<T>;
  getSessionId(): string | null;
  isRunning(): boolean;
  onInfo(info: PanelModelInfo): void;
  onError(message: string): void;
}

export function createPanelModelController(options: PanelModelControllerOptions) {
  let info: PanelModelInfo = {};
  let choices: PanelModelChoice[] = [];
  let loading = false;
  let status = "";

  const renderModelControls = () => {
    const selected = modelKey(info.provider || "", info.model || "");
    const previous = options.select.value || selected;
    options.select.replaceChildren();
    options.select.append(new Option(loading ? "Loading models..." : choices.length ? "Switch model" : "No models", ""));
    for (const choice of choices) options.select.append(new Option(choice.label, choice.value));
    options.select.value = choices.some((choice) => choice.value === selected)
      ? selected
      : choices.some((choice) => choice.value === previous)
        ? previous
        : "";
    options.select.disabled = loading || options.isRunning() || !options.getSessionId() || !choices.length;
    options.status.textContent = status || currentLabel(info);
  };

  const setInfo = (next: PanelModelInfo) => {
    info = { ...info, ...next };
    renderModelControls();
  };

  const refresh = async () => {
    const sid = options.getSessionId();
    if (!sid) return renderModelControls();
    loading = true;
    status = "Loading models";
    renderModelControls();
    try {
      const result = await options.request<PanelModelOptionsResponse>("model.options", { session_id: sid }, 30_000);
      const next = normalizeProviders(result.providers).filter((provider) => !provider.warning).flatMap((provider) =>
        provider.models.map((model) => ({
          value: modelKey(provider.slug, model),
          label: `${model} / ${provider.slug}`,
          provider: provider.slug,
          model,
        })),
      );
      choices = next;
      info = { ...info, provider: result.provider || info.provider, model: result.model || info.model };
      status = "";
      options.onInfo(info);
    } catch (err) {
      status = "Model list failed";
      options.onError(err instanceof Error ? err.message : String(err));
    } finally {
      loading = false;
      renderModelControls();
    }
  };

  const apply = async () => {
    const sid = options.getSessionId();
    const choice = choices.find((entry) => entry.value === options.select.value);
    if (!sid || !choice || choice.value === modelKey(info.provider || "", info.model || "")) return;
    loading = true;
    status = "Switching model";
    renderModelControls();
    try {
      const result = await options.request<PanelConfigSetResponse>(
        "config.set",
        { session_id: sid, key: "model", value: `${choice.model} --provider ${choice.provider}` },
        60_000,
      );
      info = { provider: choice.provider, model: result.value || choice.model };
      status = result.warning || "Model switched";
      options.onInfo(info);
    } catch (err) {
      status = "Switch failed";
      options.onError(err instanceof Error ? err.message : String(err));
    } finally {
      loading = false;
      renderModelControls();
    }
  };

  options.select.addEventListener("change", () => void apply());
  renderModelControls();
  return { render: renderModelControls, refresh, setInfo };
}

function normalizeProviders(input: unknown): PanelModelProvider[] {
  if (!Array.isArray(input)) return [];
  const providers: PanelModelProvider[] = [];
  for (const item of input) {
    if (!rec(item)) continue;
    const slug = fieldText(item.slug);
    const models = strings(item.models);
    if (!slug || !models.length) continue;
    providers.push({ name: fieldText(item.name) || slug, slug, models, warning: fieldText(item.warning) });
  }
  return providers;
}

function currentLabel(info: PanelModelInfo): string {
  return info.model ? `${info.model}${info.provider ? ` / ${info.provider}` : ""}` : "Model pending";
}

function modelKey(provider: string, model: string): string {
  return provider && model ? `${provider}\u0000${model}` : "";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string" && !!entry.trim()) : [];
}

function rec(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function fieldText(value: unknown): string {
  return typeof value === "string" ? value : "";
}
