import { strict as assert } from 'node:assert';

import { buildWhatsAppProxy, installWhatsAppProxy } from './proxy.js';

{
  const target = { fetch: 'unchanged' };
  const proxy = installWhatsAppProxy('', target);

  assert.equal(proxy.enabled, false);
  assert.equal(proxy.httpAgent, undefined);
  assert.deepEqual(proxy.versionFetchOptions, {});
  assert.deepEqual(proxy.mediaDownloadOptions, {});
  assert.equal(target.fetch, 'unchanged');
  console.log('  ✓ empty proxy configuration preserves direct networking');
}

{
  const target = {};
  const proxy = installWhatsAppProxy('http://user:pass@127.0.0.1:8080', target);

  assert.equal(proxy.enabled, true);
  assert.equal(typeof proxy.httpAgent.addRequest, 'function');
  assert.equal(target.fetch, proxy.fetch);
  assert.equal(
    proxy.mediaDownloadOptions.options.dispatcher,
    proxy.versionFetchOptions.dispatcher,
  );
  await proxy.versionFetchOptions.dispatcher.close();
  console.log('  ✓ configured proxy shares one dispatcher across Baileys fetches');
}

for (const value of ['socks5://127.0.0.1:1080', 'not a URL']) {
  assert.throws(
    () => buildWhatsAppProxy(value),
    /WHATSAPP_PROXY_URL must/,
  );
}
console.log('  ✓ unsupported and invalid proxy URLs fail closed');
