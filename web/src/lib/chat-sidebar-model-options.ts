import { api } from "./api";

/**
 * Build the model-catalog loader used by Dashboard chat.
 *
 * ModelPickerDialog calls this with ``refresh: true`` when the user requests a
 * live catalog. Preserve that option alongside the chat's profile scope so a
 * custom provider such as Bifrost reaches its ``/v1/models`` endpoint.
 */
export function chatSidebarModelOptionsLoader(profile?: string) {
  return (options?: { refresh?: boolean }) =>
    api.getModelOptions({
      profile,
      refresh: options?.refresh,
    });
}
