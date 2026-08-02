"""Real datasource connectors for Hermes Station.

Design rule (Teknium): NO fake/demo data. A source is either connected and
returns real data, or reports itself as not-connected with instructions.
Zero-key sources (local git, gh CLI, system stats, CoinGecko, open RSS,
Open-Meteo, Station's own activity DB) make the dashboard real out of the box.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UA = {"User-Agent": "HermesStation/0.2"}


def _get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 15) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "command failed").strip()[:300])
    return out.stdout


_cache: dict[str, tuple[float, dict]] = {}

def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------- connectors

def src_git_log(props: dict) -> dict:
    """Commit history of a local repo. props: {repo: path, limit}"""
    repo = os.path.expanduser(props.get("repo", "."))
    limit = int(props.get("limit", 12))
    out = _run(["git", "log", f"-{limit}", "--pretty=format:%h|%an|%ar|%s"], cwd=repo)
    rows = [l.split("|", 3) for l in out.splitlines() if l]
    return {"kind": "table", "columns": ["SHA", "Author", "When", "Message"], "rows": rows}


def src_git_status(props: dict) -> dict:
    repo = os.path.expanduser(props.get("repo", "."))
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).strip()
    dirty = len([l for l in _run(["git", "status", "--porcelain"], cwd=repo).splitlines() if l])
    ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=repo).strip() if _has_upstream(repo) else "?"
    return {"kind": "kv", "pairs": [["Branch", branch], ["Dirty files", str(dirty)],
                                    ["Ahead of upstream", ahead],
                                    ["Repo", Path(repo).name]]}

def _has_upstream(repo: str) -> bool:
    try:
        _run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=repo)
        return True
    except Exception:
        return False


def src_github_prs(props: dict) -> dict:
    """Open PRs via gh CLI. props: {repo: owner/name (optional), limit}"""
    limit = str(int(props.get("limit", 10)))
    cmd = ["gh", "pr", "list", "--limit", limit,
           "--json", "number,title,author,updatedAt,isDraft"]
    if props.get("repo"):
        cmd += ["--repo", props["repo"]]
    data = json.loads(_run(cmd, cwd=props.get("cwd") or None, timeout=20))
    rows = [[f"#{p['number']}", p["title"][:70], p["author"]["login"],
             ("draft" if p.get("isDraft") else "open"), p["updatedAt"][:10]]
            for p in data]
    return {"kind": "table", "columns": ["PR", "Title", "Author", "State", "Updated"],
            "rows": rows}


def src_github_issues(props: dict) -> dict:
    limit = str(int(props.get("limit", 10)))
    cmd = ["gh", "issue", "list", "--limit", limit, "--json", "number,title,author,updatedAt"]
    if props.get("repo"):
        cmd += ["--repo", props["repo"]]
    data = json.loads(_run(cmd, cwd=props.get("cwd") or None, timeout=20))
    rows = [[f"#{i['number']}", i["title"][:70], i["author"]["login"], i["updatedAt"][:10]]
            for i in data]
    return {"kind": "table", "columns": ["Issue", "Title", "Author", "Updated"], "rows": rows}


def src_system_stats(props: dict) -> dict:
    load1, load5, load15 = os.getloadavg()
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.strip().split()[0])
    total_gb = mem["MemTotal"] / 1048576
    avail_gb = mem["MemAvailable"] / 1048576
    du = shutil.disk_usage(os.path.expanduser("~"))
    return {"kind": "kv", "pairs": [
        ["Load (1m/5m)", f"{load1:.2f} / {load5:.2f}"],
        ["Memory", f"{total_gb-avail_gb:.1f} / {total_gb:.1f} GB"],
        ["Disk (home)", f"{(du.used/du.total)*100:.0f}% of {du.total/2**30:.0f} GB"],
        ["CPU cores", str(os.cpu_count())],
    ]}


def src_crypto_price(props: dict) -> dict:
    """CoinGecko simple price (no key). props: {coins: 'bitcoin,ethereum,solana'}"""
    coins = props.get("coins", "bitcoin,ethereum,solana")
    def fetch():
        return json.loads(_get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={coins}"
            "&vs_currencies=usd&include_24hr_change=true"))
    data = _cached(f"cg:{coins}", 30, fetch)
    rows = [[c, f"${v['usd']:,.2f}", f"{v.get('usd_24h_change', 0):+.2f}%"]
            for c, v in data.items()]
    return {"kind": "table", "columns": ["Asset", "Price", "24h"], "rows": rows}


def src_crypto_chart(props: dict) -> dict:
    """CoinGecko 7d hourly chart. props: {coin: 'bitcoin'}"""
    coin = props.get("coin", "bitcoin")
    def fetch():
        return json.loads(_get(
            f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
            "?vs_currency=usd&days=7"))
    data = _cached(f"cgc:{coin}", 300, fetch)
    pts = [[int(t / 1000), round(p, 2)] for t, p in data["prices"]]
    return {"kind": "timeseries", "label": f"{coin} USD (7d)", "points": pts}


def src_rss(props: dict) -> dict:
    """Any RSS/Atom feed. props: {url, limit}"""
    url = props.get("url", "https://hnrss.org/frontpage")
    limit = int(props.get("limit", 8))
    def fetch():
        root = ET.fromstring(_get(url))
        items = []
        for item in root.iter():
            if item.tag.endswith("item") or item.tag.endswith("entry"):
                title = link = ""
                for ch in item:
                    if ch.tag.endswith("title"):
                        title = (ch.text or "").strip()
                    elif ch.tag.endswith("link"):
                        link = (ch.text or "").strip() or ch.get("href", "")
                if title:
                    items.append({"title": title[:110], "url": link})
                if len(items) >= limit:
                    break
        return {"kind": "links", "links": items}
    return _cached(f"rss:{url}", 300, fetch)


def src_weather(props: dict) -> dict:
    """Open-Meteo (no key). props: {lat, lon, label}"""
    lat, lon = props.get("lat", 30.27), props.get("lon", -97.74)
    def fetch():
        d = json.loads(_get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,wind_speed_10m,relative_humidity_2m"
            "&daily=temperature_2m_max,temperature_2m_min&forecast_days=3"
            "&temperature_unit=fahrenheit"))
        cur = d["current"]
        days = d["daily"]
        return {"kind": "kv", "pairs": [
            ["Now", f"{cur['temperature_2m']}°F, wind {cur['wind_speed_10m']} km/h"],
            ["Humidity", f"{cur['relative_humidity_2m']}%"],
            ["Today", f"{days['temperature_2m_min'][0]}–{days['temperature_2m_max'][0]}°F"],
            ["Tomorrow", f"{days['temperature_2m_min'][1]}–{days['temperature_2m_max'][1]}°F"],
        ]}
    return _cached(f"wx:{lat},{lon}", 900, fetch)


def src_git_heatmap(props: dict) -> dict:
    """GitHub-style commit activity calendar. props: {repo, weeks}"""
    repo = os.path.expanduser(props.get("repo", "."))
    weeks = int(props.get("weeks", 16))
    out = _run(["git", "log", f"--since={weeks}.weeks", "--format=%ct"], cwd=repo)
    import datetime as _dt
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if line.strip():
            d = _dt.date.fromtimestamp(int(line.strip())).isoformat()
            counts[d] = counts.get(d, 0) + 1
    today = _dt.date.today()
    start = today - _dt.timedelta(days=today.weekday() + 7 * (weeks - 1))
    days = []
    d = start
    while d <= today:
        days.append({"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)})
        d += _dt.timedelta(days=1)
    busiest = max(counts.items(), key=lambda kv: kv[1]) if counts else None
    return {"kind": "heatmap", "days": days,
            "total": sum(counts.values()), "repo": Path(repo).name,
            "weeks": weeks,
            "range": {"start": days[0]["date"] if days else None,
                      "end": days[-1]["date"] if days else None},
            "busiest": {"date": busiest[0], "count": busiest[1]} if busiest else None}


def src_log_tail(props: dict) -> dict:
    """Live tail of a local file. props: {path, lines}"""
    path = Path(os.path.expanduser(props.get("path", ""))).resolve()
    n = min(int(props.get("lines", 40)), 200)
    if not path.is_file():
        return {"kind": "error", "error": f"no such file: {path}"}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 65536))
        tail = f.read().decode("utf-8", "replace")
    lines = tail.splitlines()[-n:]
    return {"kind": "logs", "path": str(path), "lines": lines}


def src_datadog(props: dict) -> dict:
    if not (os.getenv("DATADOG_API_KEY") and os.getenv("DATADOG_APP_KEY")):
        return _unconnected("Datadog", "Set DATADOG_API_KEY and DATADOG_APP_KEY in ~/.hermes/.env")
    q = props.get("query", "avg:system.load.1{*}")
    end = int(time.time()); start = end - 3600 * 4
    req = urllib.request.Request(
        f"https://api.datadoghq.com/api/v1/query?from={start}&to={end}&query={urllib.parse.quote(q)}",
        headers={"DD-API-KEY": os.environ["DATADOG_API_KEY"],
                 "DD-APPLICATION-KEY": os.environ["DATADOG_APP_KEY"], **UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    series = (d.get("series") or [])
    if not series:
        return {"kind": "kv", "pairs": [["Query", q], ["Result", "no data"]]}
    pts = [[int(t / 1000), round(v, 3)] for t, v in series[0]["pointlist"] if v is not None]
    return {"kind": "timeseries", "label": q, "points": pts}


def src_betterstack(props: dict) -> dict:
    tok = os.getenv("BETTERSTACK_API_TOKEN")
    if not tok:
        return _unconnected("Better Stack", "Set BETTERSTACK_API_TOKEN in ~/.hermes/.env")
    req = urllib.request.Request("https://uptime.betterstack.com/api/v2/monitors",
                                 headers={"Authorization": f"Bearer {tok}", **UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    rows = [[m["attributes"]["pronounceable_name"], m["attributes"]["status"],
             m["attributes"].get("url", "")[:40]] for m in d.get("data", [])[:10]]
    return {"kind": "table", "columns": ["Monitor", "Status", "URL"], "rows": rows}


def src_station_activity(props: dict, store=None, user_id: str = "") -> dict:
    """Station's own real activity — workflow runs + recent chats (from the DB)."""
    if store is None:
        return {"kind": "kv", "pairs": [["Activity", "store unavailable"]]}
    items = []
    for wf in store.list_workflows(user_id):
        for run in store.workflow_runs(wf["id"], limit=3):
            items.append((run["ts"], "⚙", f"{wf['name']} → {run['status']}"))
    for ev in store.events_since(user_id, time.time() - 48 * 3600):
        if ev["type"] == "chat":
            items.append((ev["ts"], "◎", (ev.get("payload") or {}).get("text", "")[:70]))
    items.sort(key=lambda i: -i[0])
    return {"kind": "feed", "items": [
        {"when": t, "icon": ic, "text": tx} for t, ic, tx in items[:14]]}


def _unconnected(name: str, how: str) -> dict:
    return {"kind": "unconnected", "source": name, "how": how}


# ---------------------------------------------------------------- dispatch

HANDLERS = {
    "git.log": src_git_log,
    "git.status": src_git_status,
    "github.prs": src_github_prs,
    "github.issues": src_github_issues,
    "system.stats": src_system_stats,
    "crypto.price": src_crypto_price,
    "crypto.chart": src_crypto_chart,
    "rss": src_rss,
    "weather": src_weather,
    "git.heatmap": src_git_heatmap,
    "log.tail": src_log_tail,
    "datadog.query": src_datadog,
    "betterstack.monitors": src_betterstack,
}

# Default refresh cadence (seconds) per source; components override with
# props.refresh_s (agent-settable via station_mutate).
DEFAULT_REFRESH = {
    "system.stats": 10,
    "station.activity": 15,
    "git.status": 20,
    "git.log": 30,
    "github.prs": 60,
    "github.issues": 60,
    "crypto.price": 45,
    "crypto.chart": 120,
    "datadog.query": 30,
    "betterstack.monitors": 60,
    "rss": 300,
    "weather": 600,
    "git.heatmap": 300,
    "log.tail": 5,
}


def query(source: str, props: dict, store=None, user_id: str = "") -> dict:
    try:
        if source == "station.activity":
            return src_station_activity(props, store=store, user_id=user_id)
        fn = HANDLERS.get(source)
        if not fn:
            return {"kind": "error", "error": f"unknown datasource: {source}"}
        return fn(props or {})
    except Exception as e:
        return {"kind": "error", "error": str(e)[:300]}


def detect_connections() -> list[dict]:
    """What real datapoints can we wire up on this machine, right now?"""
    out = []
    gh_ok = False
    try:
        _run(["gh", "auth", "status"], timeout=10)
        gh_ok = True
    except Exception:
        pass
    out.append({"id": "github", "name": "GitHub (gh CLI)", "connected": gh_ok,
                "detail": "PRs, issues, repo activity" if gh_ok else "run `gh auth login`"})
    repos = []
    for base in (Path.home() / ".hermes", Path.home() / "code", Path.home() / "projects", Path.home()):
        if base.exists():
            for p in sorted(base.iterdir()) if base != Path.home() else list(base.iterdir())[:40]:
                if (p / ".git").exists():
                    repos.append(str(p))
        if len(repos) >= 8:
            break
    out.append({"id": "git", "name": "Local git repos", "connected": bool(repos),
                "detail": f"{len(repos)} found", "repos": repos[:8]})
    out.append({"id": "system", "name": "System stats", "connected": True,
                "detail": "load, memory, disk"})
    out.append({"id": "crypto", "name": "Market data (CoinGecko)", "connected": True,
                "detail": "crypto prices + charts, no key needed"})
    out.append({"id": "rss", "name": "News / RSS feeds", "connected": True,
                "detail": "Hacker News default; any feed URL"})
    out.append({"id": "weather", "name": "Weather (Open-Meteo)", "connected": True,
                "detail": "no key needed"})
    out.append({"id": "datadog", "name": "Datadog",
                "connected": bool(os.getenv("DATADOG_API_KEY") and os.getenv("DATADOG_APP_KEY")),
                "detail": "metrics queries" if os.getenv("DATADOG_API_KEY") else "needs DATADOG_API_KEY + DATADOG_APP_KEY"})
    out.append({"id": "betterstack", "name": "Better Stack",
                "connected": bool(os.getenv("BETTERSTACK_API_TOKEN")),
                "detail": "uptime monitors" if os.getenv("BETTERSTACK_API_TOKEN") else "needs BETTERSTACK_API_TOKEN"})
    out.append({"id": "hermes", "name": "Hermes agent", "connected": True,
                "detail": "full AIAgent with your configured tools + model"})
    return out
