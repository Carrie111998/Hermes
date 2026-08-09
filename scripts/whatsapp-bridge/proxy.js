import { HttpsProxyAgent } from 'https-proxy-agent';
import { ProxyAgent as UndiciProxyAgent, fetch as undiciFetch } from 'undici';

const EMPTY_PROXY = Object.freeze({
  enabled: false,
  fetch: undefined,
  httpAgent: undefined,
  versionFetchOptions: Object.freeze({}),
  mediaDownloadOptions: Object.freeze({}),
});

export function buildWhatsAppProxy(rawUrl) {
  const value = String(rawUrl || '').trim();
  if (!value) return EMPTY_PROXY;

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('WHATSAPP_PROXY_URL must be a valid http:// or https:// URL');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('WHATSAPP_PROXY_URL must use http:// or https://');
  }

  const httpAgent = new HttpsProxyAgent(parsed);
  const dispatcher = new UndiciProxyAgent(parsed.toString());
  return {
    enabled: true,
    fetch: undiciFetch,
    httpAgent,
    versionFetchOptions: { dispatcher },
    mediaDownloadOptions: { options: { dispatcher } },
  };
}

export function installWhatsAppProxy(rawUrl, target = globalThis) {
  const proxy = buildWhatsAppProxy(rawUrl);
  if (proxy.enabled) {
    // Baileys' version and media downloads pass an Undici dispatcher. Node's
    // bundled fetch may use a different Undici instance, so keep the fetch and
    // dispatcher implementations paired.
    target.fetch = proxy.fetch;
  }
  return proxy;
}
