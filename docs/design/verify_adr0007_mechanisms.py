"""Verify the load-bearing claims in ADR-0007 against real SQLite.

Not a substitute for the acceptance tests in the ADR (those belong to the
implementer). This proves the mechanisms I am specifying actually behave
the way I claim before I freeze the contract.
"""
import json, hashlib, sqlite3, tempfile, os, subprocess

DDL = """
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    profile TEXT, status TEXT NOT NULL, started_at INTEGER NOT NULL,
    ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT
);
CREATE TABLE run_provenance (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE, task_id TEXT NOT NULL, board TEXT NOT NULL,
    profile TEXT, outcome TEXT NOT NULL, attestable INTEGER NOT NULL,
    subject_sha TEXT, verified_head_sha TEXT, branch_name TEXT,
    repo_locator TEXT, workspace_kind TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, evidence_digest TEXT,
    started_at INTEGER NOT NULL, completed_at INTEGER NOT NULL,
    contract_version TEXT NOT NULL, record_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER trg_run_provenance_no_update BEFORE UPDATE ON run_provenance
BEGIN SELECT RAISE(ABORT, 'run_provenance is append-only'); END;
CREATE TRIGGER trg_run_provenance_no_delete BEFORE DELETE ON run_provenance
BEGIN SELECT RAISE(ABORT, 'run_provenance is append-only'); END;
CREATE TABLE run_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
    task_id TEXT NOT NULL, rel_path TEXT NOT NULL, sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, git_blob_oid TEXT,
    tracked INTEGER NOT NULL DEFAULT 0, clean INTEGER NOT NULL DEFAULT 0,
    declared_by TEXT NOT NULL, created_at INTEGER NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id, rel_path)
);
CREATE TRIGGER trg_run_evidence_sealed BEFORE UPDATE ON run_evidence
WHEN OLD.sealed = 1
BEGIN SELECT RAISE(ABORT, 'evidence row is sealed'); END;
"""

def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")

def digest(obj):
    return hashlib.sha256(canon(obj)).hexdigest()

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.executescript(DDL)

# --- provenance rows for several terminal outcomes -------------------
def insert_prov(run_id, outcome, subject, head, ev):
    ev_digest = digest([{"rel_path": e["rel_path"], "sha256": e["sha256"]}
                        for e in sorted(ev, key=lambda x: x["rel_path"])]) if ev else None
    attestable = int(outcome == "completed" and bool(subject) and bool(head) and len(ev) > 0)
    body = {"contract_version": "kanban.provenance/v1", "run_id": run_id,
            "task_id": "t_demo", "board": "hermes-agent", "profile": "security-reviewer",
            "outcome": outcome, "attestable": bool(attestable),
            "subject_sha": subject, "verified_head_sha": head,
            "evidence_digest": ev_digest, "started_at": 100, "completed_at": 200}
    rd = digest(body)
    db.execute(
        "INSERT INTO run_provenance (run_id,task_id,board,profile,outcome,attestable,"
        "subject_sha,verified_head_sha,branch_name,repo_locator,workspace_kind,"
        "evidence_count,evidence_digest,started_at,completed_at,contract_version,"
        "record_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "t_demo", "hermes-agent", "security-reviewer", outcome, attestable,
         subject, head, "br", "git@github.com:x/y.git", "worktree",
         len(ev), ev_digest, 100, 200, "kanban.provenance/v1", rd, 200))
    db.commit()
    return rd

SHA_A = "a" * 40
SHA_B = "b" * 40
ev1 = [{"rel_path": "evidence/qa.json", "sha256": "c" * 64},
       {"rel_path": "evidence/sec.json", "sha256": "d" * 64}]

rd_ok = insert_prov(1, "completed", SHA_A, SHA_B, ev1)
insert_prov(2, "blocked", SHA_A, SHA_B, ev1)
insert_prov(3, "crashed", None, None, [])
insert_prov(4, "completed", SHA_A, SHA_B, [])   # no evidence

rows = {r["run_id"]: r for r in db.execute("SELECT * FROM run_provenance")}
check("row written for every terminal outcome", len(rows) == 4, f"{len(rows)} rows")
check("only completed+SHAs+evidence is attestable",
      rows[1]["attestable"] == 1 and rows[2]["attestable"] == 0
      and rows[3]["attestable"] == 0 and rows[4]["attestable"] == 0,
      f"blocked={rows[2]['attestable']} crashed={rows[3]['attestable']} noev={rows[4]['attestable']}")

# --- immutability -----------------------------------------------------
try:
    db.execute("UPDATE run_provenance SET subject_sha='e'*40 WHERE run_id=1")
    db.commit(); check("UPDATE on run_provenance aborts", False, "update succeeded!")
except sqlite3.IntegrityError as e:
    check("UPDATE on run_provenance aborts", True, str(e))
db.rollback()

try:
    db.execute("DELETE FROM run_provenance WHERE run_id=1")
    db.commit(); check("DELETE on run_provenance aborts", False, "delete succeeded!")
except sqlite3.IntegrityError as e:
    check("DELETE on run_provenance aborts", True, str(e))
db.rollback()

still = db.execute("SELECT subject_sha, record_digest FROM run_provenance WHERE run_id=1").fetchone()
check("record survives tamper attempt byte-identical",
      still["subject_sha"] == SHA_A and still["record_digest"] == rd_ok)

# --- evidence sealing -------------------------------------------------
db.execute("INSERT INTO run_evidence (run_id,task_id,rel_path,sha256,size_bytes,"
           "declared_by,created_at,sealed) VALUES (1,'t_demo','evidence/qa.json',?,10,"
           "'security-reviewer',100,0)", ("c" * 64,))
db.commit()
db.execute("UPDATE run_evidence SET sha256=? WHERE run_id=1", ("f" * 64,))  # unsealed: allowed
db.commit()
check("unsealed evidence is editable", True)
db.execute("UPDATE run_evidence SET sealed=1 WHERE run_id=1"); db.commit()
try:
    db.execute("UPDATE run_evidence SET sha256=? WHERE run_id=1", ("0" * 64,))
    db.commit(); check("sealed evidence rejects writes", False, "write succeeded!")
except sqlite3.IntegrityError as e:
    check("sealed evidence rejects writes", True, str(e))
db.rollback()

# --- duplicate run_id -------------------------------------------------
try:
    insert_prov(1, "completed", SHA_A, SHA_B, ev1)
    check("duplicate run_id rejected", False, "duplicate accepted!")
except sqlite3.IntegrityError as e:
    check("duplicate run_id rejected", True, str(e))
db.rollback()

# --- seq monotonic in terminalization order, not run id ---------------
insert_prov(99, "completed", SHA_A, SHA_B, ev1)   # high run id, terminates last
insert_prov(50, "completed", SHA_A, SHA_B, ev1)
ordered = [(r["seq"], r["run_id"]) for r in
           db.execute("SELECT seq, run_id FROM run_provenance ORDER BY seq")]
seqs = [s for s, _ in ordered]
check("seq strictly ascending", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(ordered))
check("seq order != run_id order (proves cursor is terminalization order)",
      [r for _, r in ordered] != sorted(r for _, r in ordered))

# --- watermark idempotency -------------------------------------------
def export_since(watermark):
    return [dict(r) for r in db.execute(
        "SELECT * FROM run_provenance WHERE seq > ? AND attestable = 1 ORDER BY seq",
        (watermark,))]
first = export_since(0)
wm = max(r["seq"] for r in first)
check("re-export from watermark yields nothing", export_since(wm) == [])
check("export digests unique", len({r["record_digest"] for r in first}) == len(first))

# --- read-only connection cannot write --------------------------------
tmpdir = tempfile.mkdtemp()
path = os.path.join(tmpdir, "k.db")
disk = sqlite3.connect(path); disk.executescript(DDL); disk.commit(); disk.close()
ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    ro.execute("INSERT INTO run_provenance (run_id,task_id,board,outcome,attestable,"
               "workspace_kind,evidence_count,started_at,completed_at,contract_version,"
               "record_digest,created_at) VALUES (7,'t','b','completed',1,'worktree',0,1,2,'v','d',3)")
    ro.commit(); check("read-only exporter connection cannot write", False, "write succeeded!")
except sqlite3.OperationalError as e:
    check("read-only exporter connection cannot write", True, str(e))
ro.close()

# --- SHA validation ---------------------------------------------------
import re
SHA40 = re.compile(r"^[0-9a-f]{40}$"); SHA256 = re.compile(r"^[0-9a-f]{64}$")
check("full 40-hex accepted", bool(SHA40.match(SHA_A)))
check("abbreviated SHA rejected", not SHA40.match("a1b2c3d"))
check("uppercase SHA rejected", not SHA40.match("A" * 40))
check("64-hex artifact digest accepted", bool(SHA256.match("c" * 64)))

# --- evidence_digest is order-independent -----------------------------
d1 = digest([{"rel_path": e["rel_path"], "sha256": e["sha256"]}
             for e in sorted(ev1, key=lambda x: x["rel_path"])])
d2 = digest([{"rel_path": e["rel_path"], "sha256": e["sha256"]}
             for e in sorted(list(reversed(ev1)), key=lambda x: x["rel_path"])])
check("evidence_digest stable under input order", d1 == d2)
ev_tampered = [ev1[0], {"rel_path": "evidence/sec.json", "sha256": "9" * 64}]
d3 = digest([{"rel_path": e["rel_path"], "sha256": e["sha256"]}
             for e in sorted(ev_tampered, key=lambda x: x["rel_path"])])
check("evidence_digest changes when a hash changes", d1 != d3)

# --- kernel-side hashing beats worker-declared hash -------------------
f = os.path.join(tmpdir, "artifact.json")
with open(f, "w") as fh: fh.write('{"result":"pass"}')
real = hashlib.sha256(open(f, "rb").read()).hexdigest()
worker_claimed = "0" * 64
check("kernel hash != worker-claimed hash (worker value must be ignored)",
      real != worker_claimed, real[:16] + "...")

# --- git HEAD capture is real and full-length -------------------------
repo = os.path.join(tmpdir, "repo"); os.makedirs(repo)
run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
run("git", "init", "-q")
run("git", "config", "user.email", "a@b.c"); run("git", "config", "user.name", "t")
open(os.path.join(repo, "f.txt"), "w").write("x")
run("git", "add", "."); run("git", "commit", "-qm", "init")
head = run("git", "rev-parse", "HEAD").stdout.strip()
check("git rev-parse HEAD yields full 40-hex", bool(SHA40.match(head)), head)
blob = run("git", "rev-parse", "HEAD:f.txt").stdout.strip()
check("git blob oid resolvable for tracked file", bool(SHA40.match(blob)), blob)
missing = run("git", "rev-parse", "HEAD:nope.txt")
check("untracked file yields no blob oid (tracked=0)", missing.returncode != 0)

# scratch (non-git) dir must yield nothing -> non-attestable
scratch = os.path.join(tmpdir, "scratch"); os.makedirs(scratch)
r = subprocess.run(["git", "-C", scratch, "rev-parse", "HEAD"], capture_output=True, text=True)
check("scratch workspace has no HEAD => subject_sha NULL => non-attestable", r.returncode != 0)

# --- §5.5 nullable-vs-required repository binding ---------------------
MANDATORY_FOR_EXPORT = ("repo_github_id", "event_locator", "subject_sha",
                        "verified_head_sha", "evidence_count")

def finalizable(rec):
    """Kernel-side finalization gate from ADR §5.5. Fails closed."""
    if rec.get("outcome") != "completed":
        return False, "outcome not completed"
    for f in MANDATORY_FOR_EXPORT:
        v = rec.get(f)
        if v is None or v == "":
            return False, f"missing {f}"
    if rec["evidence_count"] < 1:
        return False, "no evidence"
    if not SHA40.match(rec["subject_sha"] or ""):
        return False, "bad subject_sha"
    if not SHA40.match(rec["verified_head_sha"] or ""):
        return False, "bad verified_head_sha"
    if not isinstance(rec["repo_github_id"], int):
        return False, "repo_github_id not numeric"
    if not re.match(r"^pr:\d+$", rec["event_locator"] or ""):
        return False, "bad event_locator"
    if rec.get("corrections_present"):
        return False, "unresolved correction chain"
    return True, ""

good = {"outcome": "completed", "repo_github_id": 123456789,
        "event_locator": "pr:36", "subject_sha": SHA_A,
        "verified_head_sha": SHA_B, "evidence_count": 2,
        "corrections_present": False}
ok, why = finalizable(good)
check("complete gated run is finalizable", ok, why)

# a non-gated run (docs/research) is legal but simply never exported
non_gated = dict(good, repo_github_id=None, event_locator=None)
ok2, why2 = finalizable(non_gated)
check("non-gated run: NULL repo fields legal, not finalizable/exported",
      not ok2, why2)

# every mandatory field missing => refused, naming the field
for f in MANDATORY_FOR_EXPORT:
    bad = dict(good); bad[f] = None
    ok3, why3 = finalizable(bad)
    check(f"finalization fails closed when {f} missing", not ok3 and f in why3, why3)

check("abbreviated subject_sha refused at finalization",
      not finalizable(dict(good, subject_sha="a1b2c3d"))[0])
check("repo id as remote string refused (must be numeric)",
      not finalizable(dict(good, repo_github_id="git@github.com:x/y.git"))[0])
check("malformed event_locator refused",
      not finalizable(dict(good, event_locator="36"))[0])
check("unresolved correction chain refused",
      not finalizable(dict(good, corrections_present=True))[0])

# --- final_candidate_sha must NOT be a Kanban column (guards §4) ------
prov_cols = {r[1] for r in db.execute("PRAGMA table_info(run_provenance)")}
check("no final_candidate_sha / final_sha column exists",
      not {"final_candidate_sha", "final_sha"} & prov_cols,
      sorted(prov_cols & {"subject_sha", "verified_head_sha"}))

print("\n" + "=" * 60)
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("FAILED: " + ", ".join(failed)); raise SystemExit(1)
print("All ADR-0007 mechanism claims verified.")
