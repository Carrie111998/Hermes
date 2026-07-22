import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, rmSync, readFileSync, readdirSync, existsSync } from 'node:fs';

import { useAtomicMultiFileAuthState } from './atomic_auth_state.js';

function tempDir() {
  return mkdtempSync(path.join(os.tmpdir(), 'wa-auth-test-'));
}

test('saveCreds round-trips creds.json and leaves no .tmp behind', async () => {
  const dir = tempDir();
  try {
    const { state, saveCreds } = await useAtomicMultiFileAuthState(dir);
    state.creds.registered = true;
    await saveCreds();

    const onDisk = JSON.parse(readFileSync(path.join(dir, 'creds.json'), 'utf8'));
    assert.equal(onDisk.registered, true);
    assert.ok(!existsSync(path.join(dir, 'creds.json.tmp')));

    // Reload from disk — must resume the same creds, not mint new ones.
    const reloaded = await useAtomicMultiFileAuthState(dir);
    assert.equal(reloaded.state.creds.registered, true);
    assert.deepEqual(reloaded.state.creds.noiseKey, state.creds.noiseKey);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('concurrent saveCreds calls never leave creds.json empty or torn', async () => {
  const dir = tempDir();
  try {
    const { saveCreds } = await useAtomicMultiFileAuthState(dir);
    await Promise.all(Array.from({ length: 25 }, () => saveCreds()));

    const raw = readFileSync(path.join(dir, 'creds.json'), 'utf8');
    assert.ok(raw.length > 100, `creds.json unexpectedly small: ${raw.length}B`);
    JSON.parse(raw); // must be complete valid JSON
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('keys.set writes and keys.get reads key files; falsy value removes', async () => {
  const dir = tempDir();
  try {
    const { state } = await useAtomicMultiFileAuthState(dir);
    await state.keys.set({ 'pre-key': { 7: { private: Buffer.from('abc') } } });

    const got = await state.keys.get('pre-key', ['7']);
    assert.ok(got['7']);
    assert.deepEqual(Buffer.from(got['7'].private), Buffer.from('abc'));

    await state.keys.set({ 'pre-key': { 7: null } });
    const after = await state.keys.get('pre-key', ['7']);
    assert.equal(after['7'], null);
    assert.ok(!readdirSync(dir).some((f) => f.startsWith('pre-key-7')));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('fixFileName sanitizes ids with slashes and colons', async () => {
  const dir = tempDir();
  try {
    const { state } = await useAtomicMultiFileAuthState(dir);
    await state.keys.set({ session: { 'a/b:1': { x: 1 } } });
    assert.ok(existsSync(path.join(dir, 'session-a__b-1.json')));
    const got = await state.keys.get('session', ['a/b:1']);
    assert.equal(got['a/b:1'].x, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
