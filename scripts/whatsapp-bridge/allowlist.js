import path from 'path';
import { existsSync, readFileSync } from 'fs';

export function normalizeWhatsAppIdentifier(value) {
  return String(value || '')
    .trim()
    .replace(/:.*@/, '@')
    .replace(/@.*/, '')
    .replace(/^\+/, '');
}

export function parseAllowedUsers(rawValue) {
  return new Set(
    String(rawValue || '')
      .split(',')
      .map((value) => normalizeWhatsAppIdentifier(value))
      .filter(Boolean)
  );
}

function readMappingFile(sessionDir, identifier, suffix = '') {
  const filePath = path.join(sessionDir, `lid-mapping-${identifier}${suffix}.json`);
  if (!existsSync(filePath)) {
    return null;
  }

  try {
    const parsed = JSON.parse(readFileSync(filePath, 'utf8'));
    const normalized = normalizeWhatsAppIdentifier(parsed);
    return normalized || null;
  } catch {
    return null;
  }
}

export function expandWhatsAppIdentifiers(identifier, sessionDir) {
  const normalized = normalizeWhatsAppIdentifier(identifier);
  if (!normalized) {
    return new Set();
  }

  // Walk both phone->LID and LID->phone mapping files so allowlists can use
  // either form transparently in bot mode.
  const resolved = new Set();
  const queue = [normalized];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || resolved.has(current)) {
      continue;
    }

    resolved.add(current);

    for (const suffix of ['', '_reverse']) {
      const mapped = readMappingFile(sessionDir, current, suffix);
      if (mapped && !resolved.has(mapped)) {
        queue.push(mapped);
      }
    }
  }

  return resolved;
}

export function matchesAllowedUser(senderId, allowedUsers, sessionDir) {
  if (!allowedUsers || allowedUsers.size === 0) {
    return true;
  }

  // "*" means allow everyone (consistent with SIGNAL_GROUP_ALLOWED_USERS)
  if (allowedUsers.has('*')) {
    return true;
  }

  const aliases = expandWhatsAppIdentifiers(senderId, sessionDir);
  for (const alias of aliases) {
    if (allowedUsers.has(alias)) {
      return true;
    }
  }

  return false;
}

// Group JIDs look like "120363427286014824@g.us" — strip the suffix so the
// allowlist only has to store bare group IDs (matches WHATSAPP_ALLOWED_GROUPS docs).
export function parseAllowedGroups(rawValue) {
  return new Set(
    String(rawValue || '')
      .split(',')
      .map((value) => String(value || '').trim().replace(/@.*/, ''))
      .filter(Boolean)
  );
}

export function matchesAllowedGroup(groupJid, allowedGroups) {
  if (!allowedGroups || allowedGroups.size === 0) {
    return false;
  }

  // "*" means allow all groups (consistent with matchesAllowedUser's '*' convention)
  if (allowedGroups.has('*')) {
    return true;
  }

  const id = String(groupJid || '').replace(/@.*/, '');
  return allowedGroups.has(id);
}
