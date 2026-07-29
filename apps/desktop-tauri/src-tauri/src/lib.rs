use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use base64::Engine;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, State};

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
    std::env::var("HERMES_PROFILE").unwrap_or_else(|_| "default".into())
}

/// Run a git command in a repo. Returns stdout trimmed.
fn git_run(repo: &str, args: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(repo)
        .output()
        .map_err(|e| format!("git exec: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("git: {stderr}"));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

// ---------------------------------------------------------------------------
// Commands — Filesystem
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
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

#[allow(non_snake_case)]
#[tauri::command]
fn readFileText(path: String) -> Result<ReadFileTextResult, String> {
    let content = fs::read_to_string(&path).map_err(|e| format!("readFileText: {e}"))?;
    let size = content.len() as u64;
    Ok(ReadFileTextResult { content, size })
}

#[allow(non_snake_case)]
#[tauri::command]
fn writeTextFile(path: String, content: String) -> Result<(), String> {
    let p = Path::new(&path);
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("writeTextFile mkdir: {e}"))?;
    }
    fs::write(p, &content).map_err(|e| format!("writeTextFile: {e}"))?;
    Ok(())
}

#[allow(non_snake_case)]
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

// ---------------------------------------------------------------------------
// Commands — Git
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
#[tauri::command]
fn gitRoot(startPath: String) -> Result<GitRootResult, String> {
    let root = git_run(&startPath, &["rev-parse", "--show-toplevel"])?;
    Ok(GitRootResult { root })
}

/// Scan a directory for git repositories.
#[allow(non_snake_case)]
#[tauri::command]
fn scanRepos(dir: String) -> Result<Vec<String>, String> {
    let mut repos = Vec::new();
    let entries = fs::read_dir(&dir).map_err(|e| format!("scanRepos: {e}"))?;
    for entry in entries {
        let entry = entry.map_err(|e| format!("scanRepos entry: {e}"))?;
        let p = entry.path();
        if p.join(".git").exists() {
            repos.push(p.to_string_lossy().to_string());
        }
    }
    repos.sort();
    Ok(repos)
}

/// Get repo status (branch + changes as parsed porcelain).
#[allow(non_snake_case)]
#[tauri::command]
fn repoStatus(path: String) -> Result<Value, String> {
    let branch = git_run(&path, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap_or_default();
    let porcelain = git_run(&path, &["status", "--porcelain"]).unwrap_or_default();
    let mut changes = Vec::new();
    for line in porcelain.lines() {
        if line.len() < 3 {
            continue;
        }
        let status_raw = &line[..2];
        let file = line[3..].trim().to_string();
        let staged = matches!(status_raw.chars().next(), Some('M' | 'A' | 'D' | 'R' | 'C'));
        let unstaged = matches!(status_raw.chars().nth(1), Some('M' | 'A' | 'D' | '?'));
        changes.push(serde_json::json!({
            "path": file,
            "staged": staged,
            "unstaged": unstaged,
            "status": status_raw,
        }));
    }
    Ok(serde_json::json!({
        "branch": branch,
        "changes": changes,
    }))
}

/// List branches.
#[allow(non_snake_case)]
#[tauri::command]
fn branchList(path: String) -> Result<Vec<Value>, String> {
    let output = git_run(&path, &["branch", "--list", "--format=%(refname:short)|%(HEAD)"]).unwrap_or_default();
    Ok(output.lines().map(|line| {
        let parts: Vec<&str> = line.split('|').collect();
        serde_json::json!({
            "name": parts.first().unwrap_or(&""),
            "current": parts.get(1).unwrap_or(&" ") == &"*",
        })
    }).collect())
}

/// Switch to a branch.
#[allow(non_snake_case)]
#[tauri::command]
fn branchSwitch(path: String, name: String) -> Result<(), String> {
    git_run(&path, &["switch", &name])?;
    Ok(())
}

/// Get file diff (unstaged).
#[allow(non_snake_case)]
#[tauri::command]
fn fileDiff(path: String, file: String) -> Result<String, String> {
    git_run(&path, &["diff", "--no-color", &file])
}

/// List changed files for review tab.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewList(path: String) -> Result<Vec<Value>, String> {
    let output = git_run(&path, &["status", "--porcelain"]).unwrap_or_default();
    Ok(output.lines().filter(|l| l.len() >= 3).map(|line| {
        let index = line.chars().next().unwrap_or(' ').to_string();
        let working = line.chars().nth(1).unwrap_or(' ').to_string();
        let file = line[3..].trim().to_string();
        serde_json::json!({
            "path": file,
            "index": index,
            "workingTree": working,
        })
    }).collect())
}

/// Get staged or unstaged diff for a specific file.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewDiff(path: String, file: String, staged: Option<bool>) -> Result<String, String> {
    if staged.unwrap_or(false) {
        git_run(&path, &["diff", "--cached", "--no-color", &file])
    } else {
        git_run(&path, &["diff", "--no-color", &file])
    }
}

/// Stage files.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewStage(path: String, files: Vec<String>) -> Result<(), String> {
    let mut args = vec!["add", "--"];
    for f in &files {
        args.push(f.as_str());
    }
    git_run(&path, &args)?;
    Ok(())
}

/// Unstage files.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewUnstage(path: String, files: Vec<String>) -> Result<(), String> {
    let mut args = vec!["restore", "--staged", "--"];
    for f in &files {
        args.push(f.as_str());
    }
    git_run(&path, &args)?;
    Ok(())
}

/// Revert (checkout) files.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewRevert(path: String, files: Vec<String>) -> Result<(), String> {
    let mut args = vec!["checkout", "--"];
    for f in &files {
        args.push(f.as_str());
    }
    git_run(&path, &args)?;
    Ok(())
}

/// Parse a git revision.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewRevParse(path: String, rev: String) -> Result<String, String> {
    git_run(&path, &["rev-parse", &rev])
}

/// Commit staged changes.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewCommit(path: String, message: String) -> Result<(), String> {
    git_run(&path, &["commit", "-m", &message])?;
    Ok(())
}

/// Push to remote.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewPush(path: String) -> Result<(), String> {
    git_run(&path, &["push"])?;
    Ok(())
}

/// Get last commit context.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewCommitContext(path: String) -> Result<Value, String> {
    let hash = git_run(&path, &["log", "-1", "--format=%H"]).unwrap_or_default();
    let subject = git_run(&path, &["log", "-1", "--format=%s"]).unwrap_or_default();
    let author = git_run(&path, &["log", "-1", "--format=%an"]).unwrap_or_default();
    Ok(serde_json::json!({
        "hash": hash,
        "subject": subject,
        "author": author,
    }))
}

/// Get upstream tracking info.
#[allow(non_snake_case)]
#[tauri::command]
fn reviewShipInfo(path: String) -> Result<Value, String> {
    let upstream = git_run(&path, &["rev-parse", "--abbrev-ref", "@{upstream}"]).ok();
    let ahead = git_run(&path, &["rev-list", "--count", "@{upstream}..HEAD"]).ok();
    let behind = git_run(&path, &["rev-list", "--count", "HEAD..@{upstream}"]).ok();
    Ok(serde_json::json!({
        "upstream": upstream.unwrap_or_default(),
        "ahead": ahead.and_then(|s| s.parse::<i32>().ok()).unwrap_or(0),
        "behind": behind.and_then(|s| s.parse::<i32>().ok()).unwrap_or(0),
    }))
}

/// List likely base branches (main/master/develop/dev).
#[allow(non_snake_case)]
#[tauri::command]
fn baseBranchList(path: String) -> Result<Vec<String>, String> {
    let output = git_run(&path, &["branch", "--list", "main", "master", "develop", "dev"]).unwrap_or_default();
    Ok(output.lines().map(|l| l.trim().to_string()).filter(|l| !l.is_empty()).collect())
}

// ---------------------------------------------------------------------------
// Commands — Navigation / OS
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
#[tauri::command]
fn revealPath(path: String) -> Result<(), String> {
    let p = Path::new(&path);
    let dir = if p.is_dir() { p } else { p.parent().unwrap_or(p) };
    open::that_in_background(dir);
    Ok(())
}

#[allow(non_snake_case)]
#[tauri::command]
fn renamePath(path: String, newName: String) -> Result<(), String> {
    let src = Path::new(&path);
    let dest = src
        .parent()
        .map(|p| p.join(&newName))
        .unwrap_or_else(|| PathBuf::from(&newName));
    fs::rename(src, &dest).map_err(|e| format!("renamePath: {e}"))
}

#[allow(non_snake_case)]
#[tauri::command]
fn trashPath(path: String) -> Result<(), String> {
    trash::delete(&path).map_err(|e| format!("trashPath: {e}"))
}

// ---------------------------------------------------------------------------
// Commands — Clipboard
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
#[tauri::command]
async fn writeClipboard(app: AppHandle, text: String) -> Result<bool, String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    app.clipboard()
        .write_text(text)
        .map_err(|e| format!("writeClipboard: {e}"))?;
    Ok(true)
}

#[allow(non_snake_case)]
#[tauri::command]
fn saveClipboardImage() -> Result<String, String> {
    let output = Command::new("xclip")
        .args(["-selection", "clipboard", "-t", "image/png", "-o"])
        .output()
        .map_err(|e| format!("xclip exec: {e}"))?;
    if !output.status.success() || output.stdout.is_empty() {
        return Ok(String::new());
    }
    let png_sig: &[u8] = &[0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
    if output.stdout.len() < 8 || &output.stdout[..8] != png_sig {
        return Ok(String::new());
    }
    let pid = std::process::id();
    let path = format!("/tmp/hermes-clipboard-{}.png", pid);
    std::fs::write(&path, &output.stdout).map_err(|e| format!("saveClipboardImage write: {e}"))?;
    Ok(path)
}

// ---------------------------------------------------------------------------
// Commands — Image I/O
// ---------------------------------------------------------------------------

/// Download an image URL to a temp file. Returns true on success.
#[allow(non_snake_case)]
#[tauri::command]
async fn saveImageFromUrl(url: String) -> Result<bool, String> {
    let resp = reqwest::get(&url).await.map_err(|e| format!("saveImageFromUrl fetch: {e}"))?;
    let bytes = resp.bytes().await.map_err(|e| format!("saveImageFromUrl read: {e}"))?;
    let ext = url.rsplit('.').next().unwrap_or("png").split('?').next().unwrap_or("png");
    let pid = std::process::id();
    let ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    let path = format!("/tmp/hermes-image-{}-{}.{}", pid, ts, ext);
    std::fs::write(&path, &bytes).map_err(|e| format!("saveImageFromUrl write: {e}"))?;
    Ok(true)
}

/// Save raw image buffer (from paste/drag) to a temp file. Returns file path.
#[allow(non_snake_case)]
#[tauri::command]
fn saveImageBuffer(data: Vec<u8>, ext: String) -> Result<String, String> {
    let pid = std::process::id();
    let ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    let path = format!("/tmp/hermes-image-{}-{}.{}", pid, ts, ext);
    std::fs::write(&path, &data).map_err(|e| format!("saveImageBuffer write: {e}"))?;
    Ok(path)
}

// ---------------------------------------------------------------------------
// Commands — Dialogs
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
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

// ---------------------------------------------------------------------------
// Commands — Connection
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
#[tauri::command]
async fn getConnection(
    state: State<'_, AppState>,
    profile: Option<String>,
) -> Result<HermesConnection, String> {
    let active = profile.unwrap_or_else(|| state.active_profile.clone());
    let SESSION_TOKEN = fetch_session_token().await.unwrap_or_default();
    Ok(HermesConnection {
        profile: active.clone(),
        status: "connected".into(),
        ws_url: Some(format!("ws://127.0.0.1:44985/api/ws?token={}", SESSION_TOKEN)),
        api_url: Some(format!("http://127.0.0.1:44985/api?profile={}", active)),
    })
}

async fn fetch_session_token() -> Option<String> {
    if let Ok(token) = std::env::var("HERMES_DASHBOARD_SESSION_TOKEN") {
        if !token.is_empty() {
            return Some(token);
        }
    }
    let html = reqwest::get("http://127.0.0.1:44985/").await.ok()?.text().await.ok()?;
    let re = Regex::new(r#"__HERMES_SESSION_TOKEN__="([^"]+)""#).ok()?;
    re.captures(&html)?.get(1).map(|m| m.as_str().to_string())
}

// ---------------------------------------------------------------------------
// Commands — External / API
// ---------------------------------------------------------------------------

#[allow(non_snake_case)]
#[tauri::command]
async fn openExternal(url: String) -> Result<(), String> {
    open::that_in_background(&url);
    Ok(())
}

#[allow(non_snake_case)]
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
            scanRepos,
            repoStatus,
            branchList,
            branchSwitch,
            fileDiff,
            reviewList,
            reviewDiff,
            reviewStage,
            reviewUnstage,
            reviewRevert,
            reviewRevParse,
            reviewCommit,
            reviewPush,
            reviewCommitContext,
            reviewShipInfo,
            baseBranchList,
            revealPath,
            renamePath,
            trashPath,
            writeClipboard,
            saveClipboardImage,
            saveImageFromUrl,
            saveImageBuffer,
            selectPaths,
            getConnection,
            openExternal,
            api,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
