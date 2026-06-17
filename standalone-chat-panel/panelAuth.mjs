import { randomBytes, pbkdf2Sync, timingSafeEqual, createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";

const PASSWORD_ITERATIONS = 310_000;
const TOKEN_BYTES = 32;
const SESSION_TTL_MS = 12 * 60 * 60 * 1000;

export function createPanelAuthStore(options = {}) {
  const driver = String(options.driver || process.env.HERMES_PANEL_AUTH_DRIVER || "sqlite").trim().toLowerCase();
  if (driver !== "sqlite") {
    throw new Error(`Unsupported HERMES_PANEL_AUTH_DRIVER=${driver}. sqlite is implemented; mysql adapter slot is reserved.`);
  }
  const dbPath = resolve(
    String(
      options.dbPath
        || process.env.HERMES_PANEL_AUTH_DB
        || process.env.ULTRA_STUDIO_AUTH_DB
        || "./.ultra-studio-panel-auth.sqlite",
    ),
  );
  return new SQLitePanelAuthStore(dbPath);
}

export class SQLitePanelAuthStore {
  constructor(dbPath) {
    this.dbPath = dbPath;
    mkdirSync(dirname(dbPath), { recursive: true });
    this.db = new DatabaseSync(dbPath);
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec("PRAGMA foreign_keys = ON");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS panel_users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        label TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        roles TEXT NOT NULL,
        disabled_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS panel_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_panel_sessions_user_id ON panel_sessions(user_id);
      CREATE INDEX IF NOT EXISTS idx_panel_sessions_expires_at ON panel_sessions(expires_at);
    `);
    this.migrateBootstrapOwnerRole();
  }

  userCount() {
    return Number(this.db.prepare("SELECT COUNT(*) AS n FROM panel_users WHERE disabled_at IS NULL").get().n || 0);
  }

  createUser(input) {
    const username = normalizeUsername(input.username);
    if (!username) throw new Error("username_required");
    const password = String(input.password || "");
    if (password.length < 8) throw new Error("password_too_short");
    const now = Date.now();
    const id = `usr_${randomBytes(12).toString("hex")}`;
    const salt = randomBytes(16).toString("base64url");
    const hash = hashPassword(password, salt);
    const label = cleanField(input.label) || username;
    const tenantId = cleanId(input.tenant_id) || cleanId(process.env.HERMES_PANEL_DEFAULT_TENANT_ID) || `tenant-${username}`;
    const workspaceId = cleanId(input.workspace_id) || cleanId(input.workspace) || `workspace-${username}`;
    const projectId = cleanId(input.project_id) || cleanId(process.env.HERMES_PANEL_DEFAULT_PROJECT_ID) || "project-default";
    const userId = cleanId(input.user_id) || `user-${username}`;
    const roles = normalizeRoles(input.roles) || defaultRolesForCreate(this.userCount());
    this.db.prepare(`
      INSERT INTO panel_users (
        id, username, password_salt, password_hash, label,
        tenant_id, workspace_id, project_id, user_id, roles,
        created_at, updated_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(id, username, salt, hash, label, tenantId, workspaceId, projectId, userId, roles, now, now);
    return this.publicUser(this.getUserById(id));
  }

  login(usernameInput, password) {
    this.pruneExpiredSessions();
    const username = normalizeUsername(usernameInput);
    const user = this.getUserByUsername(username);
    if (!user || user.disabled_at) return null;
    const expected = Buffer.from(String(user.password_hash), "base64url");
    const actual = Buffer.from(hashPassword(String(password || ""), String(user.password_salt)), "base64url");
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return null;
    const token = `usp_${randomBytes(TOKEN_BYTES).toString("base64url")}`;
    const tokenHash = hashToken(token);
    const now = Date.now();
    this.db.prepare(`
      INSERT INTO panel_sessions (token_hash, user_id, expires_at, created_at, last_seen_at)
      VALUES (?, ?, ?, ?, ?)
    `).run(tokenHash, user.id, now + SESSION_TTL_MS, now, now);
    return { token, user: this.publicUser(user) };
  }

  verifyToken(rawToken) {
    this.pruneExpiredSessions();
    const token = String(rawToken || "").replace(/^Bearer\s+/i, "").trim();
    if (!token.startsWith("usp_")) return null;
    const tokenHash = hashToken(token);
    const row = this.db.prepare(`
      SELECT u.*
      FROM panel_sessions s
      JOIN panel_users u ON u.id = s.user_id
      WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled_at IS NULL
    `).get(tokenHash, Date.now());
    if (!row) return null;
    this.db.prepare("UPDATE panel_sessions SET last_seen_at = ? WHERE token_hash = ?").run(Date.now(), tokenHash);
    return row;
  }

  revokeToken(rawToken) {
    const token = String(rawToken || "").replace(/^Bearer\s+/i, "").trim();
    if (!token) return;
    this.db.prepare("DELETE FROM panel_sessions WHERE token_hash = ?").run(hashToken(token));
  }

  publicUser(user) {
    if (!user) return null;
    return {
      username: user.username,
      label: user.label,
      tenant_id: user.tenant_id,
      workspace: user.workspace_id,
      workspace_id: user.workspace_id,
      project_id: user.project_id,
      user_id: user.user_id,
      roles: String(user.roles || "")
        .split(",")
        .map((role) => role.trim())
        .filter(Boolean),
    };
  }

  principalHeaders(user, apiServerKey) {
    return {
      Authorization: `Bearer ${apiServerKey}`,
      "X-Hermes-Tenant-Id": String(user.tenant_id),
      "X-Hermes-Workspace-Id": String(user.workspace_id),
      "X-Hermes-Project-Id": String(user.project_id),
      "X-Hermes-User-Id": String(user.user_id),
      "X-Hermes-Roles": String(user.roles || "creator"),
    };
  }

  getUserByUsername(username) {
    if (!username) return null;
    return this.db.prepare("SELECT * FROM panel_users WHERE username = ?").get(username) || null;
  }

  getUserById(id) {
    return this.db.prepare("SELECT * FROM panel_users WHERE id = ?").get(id) || null;
  }

  pruneExpiredSessions() {
    this.db.prepare("DELETE FROM panel_sessions WHERE expires_at <= ?").run(Date.now());
  }

  migrateBootstrapOwnerRole() {
    const row = this.db.prepare(`
      SELECT id, roles
      FROM panel_users
      WHERE disabled_at IS NULL
      ORDER BY created_at ASC
      LIMIT 1
    `).get();
    if (!row) return;
    const roles = new Set(normalizeRoles(row.roles).split(",").filter(Boolean));
    if (roles.has("owner") || roles.has("admin")) return;
    roles.add("owner");
    roles.add("creator");
    roles.add("member");
    this.db.prepare("UPDATE panel_users SET roles = ?, updated_at = ? WHERE id = ?")
      .run([...roles].join(","), Date.now(), row.id);
  }
}

export function readBearerToken(req) {
  const auth = String(req.headers.authorization || "");
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  return "";
}

function normalizeUsername(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]/g, "")
    .slice(0, 64);
}

function cleanField(value) {
  return String(value || "").replace(/[\r\n\x00]/g, " ").trim().slice(0, 128);
}

function cleanId(value) {
  return String(value || "")
    .trim()
    .replace(/[^A-Za-z0-9_.:-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 96);
}

function normalizeRoles(value) {
  if (Array.isArray(value)) {
    return value.map(cleanId).filter(Boolean).slice(0, 16).join(",");
  }
  return String(value || "")
    .split(",")
    .map(cleanId)
    .filter(Boolean)
    .slice(0, 16)
    .join(",");
}

function defaultRolesForCreate(existingUserCount) {
  return existingUserCount === 0 ? "owner,creator,member" : "creator";
}

function hashPassword(password, salt) {
  return pbkdf2Sync(password, salt, PASSWORD_ITERATIONS, 32, "sha256").toString("base64url");
}

function hashToken(token) {
  return createHash("sha256").update(String(token)).digest("base64url");
}
