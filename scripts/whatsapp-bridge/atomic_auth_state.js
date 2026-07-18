// Drop-in replacement for Baileys' useMultiFileAuthState with ATOMIC writes.
//
// Baileys' stock implementation writes creds.json (and every key file) via a
// plain writeFile — an open-truncate-then-write. Any crash or kill landing in
// that window leaves a 0-byte creds.json and the linked device is lost until
// a manual re-pair. This has zeroed the session three times on this machine
// (2026-06-11 ENOSPC, 2026-07-10 kill mid-save, 2026-07-18 kill mid-save —
// risk register R70).
//
// Here every write goes to `<file>.tmp` first and is renamed over the target,
// so the target always holds either the complete old or the complete new
// content. rename() on Windows can transiently fail with EPERM/EACCES when a
// concurrent reader (e.g. the laptop-monitor creds probe) holds the target
// open without FILE_SHARE_DELETE — retried with a short bounded backoff.

import { mkdir, readFile, rename, stat, unlink, writeFile } from 'fs/promises';
import { join } from 'path';
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys';

const RENAME_RETRIES = 6;
const RENAME_RETRY_DELAY_MS = 50;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Minimal per-file async lock (promise chaining) — mirrors the mutex Baileys
// uses, without depending on their transitive async-mutex package.
const fileLocks = new Map();
function withFileLock(filePath, fn) {
  const prev = fileLocks.get(filePath) || Promise.resolve();
  const next = prev.then(fn, fn);
  // Keep the chain from growing unboundedly: clear once settled and current.
  fileLocks.set(filePath, next.catch(() => {}));
  return next;
}

async function atomicWriteFile(filePath, contents) {
  const tmpPath = `${filePath}.tmp`;
  await writeFile(tmpPath, contents);
  for (let attempt = 1; ; attempt += 1) {
    try {
      await rename(tmpPath, filePath);
      return;
    } catch (err) {
      const transient = err && (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY');
      if (!transient || attempt >= RENAME_RETRIES) {
        // Leave the .tmp behind — it holds the complete new content and is
        // the recovery artifact if the target is ever found truncated.
        throw err;
      }
      await sleep(RENAME_RETRY_DELAY_MS * attempt);
    }
  }
}

export async function useAtomicMultiFileAuthState(folder) {
  const fixFileName = (file) => file?.replace(/\//g, '__')?.replace(/:/g, '-');

  const writeData = (data, file) => {
    const filePath = join(folder, fixFileName(file));
    return withFileLock(filePath, () =>
      atomicWriteFile(filePath, JSON.stringify(data, BufferJSON.replacer))
    );
  };

  const readData = async (file) => {
    try {
      const filePath = join(folder, fixFileName(file));
      return await withFileLock(filePath, async () => {
        const data = await readFile(filePath, { encoding: 'utf-8' });
        return JSON.parse(data, BufferJSON.reviver);
      });
    } catch {
      return null;
    }
  };

  const removeData = async (file) => {
    try {
      const filePath = join(folder, fixFileName(file));
      await withFileLock(filePath, async () => {
        try {
          await unlink(filePath);
        } catch {}
      });
    } catch {}
  };

  const folderInfo = await stat(folder).catch(() => {});
  if (folderInfo) {
    if (!folderInfo.isDirectory()) {
      throw new Error(
        `found something that is not a directory at ${folder}, either delete it or specify a different location`
      );
    }
  } else {
    await mkdir(folder, { recursive: true });
  }

  const creds = (await readData('creds.json')) || initAuthCreds();

  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          await Promise.all(
            ids.map(async (id) => {
              let value = await readData(`${type}-${id}.json`);
              if (type === 'app-state-sync-key' && value) {
                value = proto.Message.AppStateSyncKeyData.fromObject(value);
              }
              data[id] = value;
            })
          );
          return data;
        },
        set: async (data) => {
          const tasks = [];
          for (const category in data) {
            for (const id in data[category]) {
              const value = data[category][id];
              const file = `${category}-${id}.json`;
              tasks.push(value ? writeData(value, file) : removeData(file));
            }
          }
          await Promise.all(tasks);
        },
      },
    },
    saveCreds: async () => {
      return writeData(creds, 'creds.json');
    },
  };
}
