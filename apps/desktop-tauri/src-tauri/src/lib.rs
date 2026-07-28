use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use base64::Engine;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Manager, State};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct DirEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
}

#[derive(Debug, Serialize)]
pub struct ReadFileTextResult {
    pub content: String,
    pub size: u64,
}

#[derive(Debug, Deserialize)]
pub struct HermesApiRequest {
    pub method: String,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub headers: Option<Vec<(String, String)>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body: Option<Value>,
}

#[derive(Debug, Serialize)]
pub struct HermesConnection {
    pub profile: String,
    pub status: String,
    #[serde(rename = "wsUrl")]
    pub ws_url: Option<String>,
    #[serde(rename = "apiUrl")]
    pub api_url: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct GitRootResult {
    pub root: String,
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

pub struct AppState {
    pub active_profile: String,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn default_profile() -> String {
    // HERMES_PROFILE env var overrides the hardcoded default.
    // Matches the profile selection logic in hermes serve.
    std::env::var("HERMES_PROFILE").unwrap_or_else(|_| "default".into())
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

#[tauri::command]
fn readDir(path: String) -> Result<Vec<DirEntry>, String> {
    let entries = fs::read_dir(&path).map_err(|e| format!("readDir: {e}"))?;
    let mut result = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| format!("readDir entry: {e}"))?;
        let meta = entry.metadata().map_err(|e| format!("readDir metadata: {e}"))?;
        result.push(DirEntry {
            name: entry.file_name().to_string_lossy().into_owned(),
            path: entry.path().to_string_lossy().into_owned(),
            is_dir: meta.is_dir(),
            size: meta.len(),
        });
    }
    result.sort_by(|a, b| b.is_dir.cmp(&a.is_dir).then(a.name.cmp(&b.name)));
    Ok(result)
}

#[tauri::command]
fn readFileText(path: String) -> Result<ReadFileTextResult, String> {
    let content = fs::read_to_string(&path).map_err(|e| format!("readFileText: {e}"))?;
    let size = content.len() as u64;
    Ok(ReadFileTextResult { content, size })
}

#[tauri::command]
fn writeTextFile(path: String, content: String) -> Result<(), String> {
    let p = Path::new(&path);
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("writeTextFile mkdir: {e}"))?;
    }
    fs::write(p, &content).map_err(|e| format!("writeTextFile: {e}"))?;
    Ok(())
}

#[tauri::command]
fn readFileDataUrl(path: String) -> Result<String, String> {
    let data = fs::read(&path).map_err(|e| format!("readFileDataUrl: {e}"))?;
    let ext = Path::new(&path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("");
    let mime = match ext.to_lowercase().as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "svg" => "image/svg+xml",
        "ico" => "image/x-icon",
        "pdf" => "application/pdf",
        "json" => "application/json",
        "html" | "htm" => "text/html",
        "css" => "text/css",
        "js" | "mjs" => "text/javascript",
        "txt" | "md" => "text/plain",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        _ => "application/octet-stream",
    };
    let b64 = base64::engine::general_purpose::STANDARD.encode(&data);
    Ok(format!("data:{};base64,{}", mime, b64))
}

#[tauri::command]
fn gitRoot(startPath: String) -> Result<GitRootResult, String> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(&startPath)
        .output()
        .map_err(|e| format!("gitRoot exec: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("gitRoot: {stderr}"));
    }
    let root = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok(GitRootResult { root })
}

#[tauri::command]
fn revealPath(path: String) -> Result<(), String> {
    let p = Path::new(&path);
    let dir = if p.is_dir() { p } else { p.parent().unwrap_or(p) };
    open::that_in_background(dir);
    Ok(())
}

#[tauri::command]
fn renamePath(path: String, newName: String) -> Result<(), String> {
    let src = Path::new(&path);
    let dest = src
        .parent()
        .map(|p| p.join(&newName))
        .unwrap_or_else(|| PathBuf::from(&newName));
    fs::rename(src, &dest).map_err(|e| format!("renamePath: {e}"))
}

#[tauri::command]
fn trashPath(path: String) -> Result<(), String> {
    trash::delete(&path).map_err(|e| format!("trashPath: {e}"))
}

#[tauri::command]
async fn writeClipboard(app: AppHandle, text: String) -> Result<bool, String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    app.clipboard()
        .write_text(text)
        .map_err(|e| format!("writeClipboard: {e}"))?;
    Ok(true)
}

// Tauri v2 dialog API uses callbacks
#[tauri::command]
async fn selectPaths(
    app: AppHandle,
    options: Option<Value>,
) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let opts = options.unwrap_or_default();
    let (tx, rx) = std::sync::mpsc::channel();

    if opts.get("directories").and_then(|v| v.as_bool()).unwrap_or(false) {
        app.dialog().file().pick_folder(move |file| {
            let _ = tx.send(file.map(|f| vec![f.to_string()]).unwrap_or_default());
        });
    } else {
        app.dialog().file().pick_files(move |files| {
            let paths = files.unwrap_or_default().into_iter().map(|f| f.to_string()).collect();
            let _ = tx.send(paths);
        });
    }

    rx.recv().map_err(|e| format!("selectPaths: dialog cancelled: {e}"))
}

#[tauri::command]
async fn getConnection(
    state: State<'_, AppState>,
    profile: Option<String>,
) -> Result<HermesConnection, String> {
    let active = profile.unwrap_or_else(|| state.active_profile.clone());
    // ponytail: serve backend on 44985, fetch session token for WebSocket auth
    let SESSION_TOKEN = fetch_session_token().await.unwrap_or_default();
    Ok(HermesConnection {
        profile: active.clone(),
        status: "connected".into(),
        ws_url: Some(format!("ws://127.0.0.1:44985/api/ws?token={}", SESSION_TOKEN)),
        api_url: Some(format!("http://127.0.0.1:44985/api?profile={}", active)),
    })
}

/// Fetch __HERMES_SESSION_TOKEN__ from the backend.
/// Priority: HERMES_DASHBOARD_SESSION_TOKEN env var (set by launcher), then HTML page.
async fn fetch_session_token() -> Option<String> {
    // Fast path: launcher set the token as env var (hermes serve with HERMES_DASHBOARD_SESSION_TOKEN).
    if let Ok(token) = std::env::var("HERMES_DASHBOARD_SESSION_TOKEN") {
        if !token.is_empty() {
            return Some(token);
        }
    }
    // Fallback: parse from backend HTML page (legacy path for Electron).
    let html = reqwest::get("http://127.0.0.1:44985/").await.ok()?.text().await.ok()?;
    let re = regex::Regex::new(r#"__HERMES_SESSION_TOKEN__="([^"]+)""#).ok()?;
    re.captures(&html)?.get(1).map(|m| m.as_str().to_string())
}

#[tauri::command]
async fn openExternal(url: String) -> Result<(), String> {
    open::that_in_background(&url);
    Ok(())
}

#[tauri::command]
async fn api(request: HermesApiRequest) -> Result<Value, String> {
    let client = reqwest::Client::new();
    let method = request.method.to_uppercase();
    let mut req = match method.as_str() {
        "GET" => client.get(&request.url),
        "POST" => client.post(&request.url),
        "PUT" => client.put(&request.url),
        "PATCH" => client.patch(&request.url),
        "DELETE" => client.delete(&request.url),
        "HEAD" => client.head(&request.url),
        _ => return Err(format!("api: unsupported method {}", request.method)),
    };
    if let Some(headers) = &request.headers {
        for (k, v) in headers {
            req = req.header(k, v);
        }
    }
    // Add session token for backend auth (populated by launcher as HERMES_DASHBOARD_SESSION_TOKEN).
    if let Ok(token) = std::env::var("HERMES_DASHBOARD_SESSION_TOKEN") {
        if !token.is_empty() {
            req = req.header("X-Hermes-Session-Token", &token);
        }
    }
    if let Some(body) = &request.body {
        req = req.json(body);
    }
    let resp = req.send().await.map_err(|e| format!("api request: {e}"))?;
    let status = resp.status();
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if content_type.contains("json") {
        let json: Value = resp.json().await.map_err(|e| format!("api json: {e}"))?;
        Ok(serde_json::json!({"status": status.as_u16(), "data": json}))
    } else {
        let text = resp.text().await.map_err(|e| format!("api text: {e}"))?;
        Ok(serde_json::json!({"status": status.as_u16(), "data": text}))
    }
}

// ---------------------------------------------------------------------------
// App Builder
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(AppState {
            active_profile: default_profile(),
        })
        .invoke_handler(tauri::generate_handler![
            readDir,
            readFileText,
            writeTextFile,
            readFileDataUrl,
            gitRoot,
            revealPath,
            renamePath,
            trashPath,
            writeClipboard,
            selectPaths,
            getConnection,
            openExternal,
            api,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
