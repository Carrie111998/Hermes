"""Durable policy-only GPT -> local Qwen fallback state and orchestration."""
from __future__ import annotations

import contextlib, contextvars, hashlib, json, logging, os, re, sqlite3
import threading, time, unicodedata, uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)
SUMMARY_MAX, RESULT_MAX = 8192, 32768
ARTIFACT_TTL, ROW_TTL, LEASE_TTL = 7*86400, 30*86400, 3600
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_REVISION = "bf3bf13ab40c3157080a7ab344c831b9ad18b5eb"
SIMILARITY_LIMIT = .88
TERMINAL = {"completed", "partial", "failed", "timeout"}
_runtime = contextvars.ContextVar("policy_fallback_runtime", default=None)
_embedder = None
_embed_failed = False
_cleanup_guard, _last_cleanup = threading.Lock(), 0.0

def clip(x, n=SUMMARY_MAX):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return s[:n]

def sha(s): return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()
def canonical(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def normalize_question(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cc","Cf"} or c in "\n\t").casefold()
    def url(m):
        try:
            p=urlsplit(m.group(0).rstrip(".,);]}")); host=(p.hostname or "").rstrip(".").casefold()
            return urlunsplit(((p.scheme or "https").casefold(), host+(f":{p.port}" if p.port else ""), re.sub(r"/{2,}","/",p.path or "/"), "", ""))
        except Exception: return m.group(0)
    return re.sub(r"\s+", " ", re.sub(r"https?://[^\s<>'\"]+", url, text, flags=re.I)).strip()

def question_hashes(question, evidence):
    norm=normalize_question(question); eh=sha(canonical(evidence or []))
    return norm, sha(norm), eh, sha(norm+":"+eh)

def embedding(norm):
    global _embedder, _embed_failed
    if _embed_failed: return None
    try:
        if _embedder is None:
            from sentence_transformers import SentenceTransformer
            _embedder=SentenceTransformer(EMBED_MODEL, revision=EMBED_REVISION, device="cpu")
        return _embedder.encode([norm], normalize_embeddings=True)[0].astype("float32").tobytes()
    except Exception:
        _embed_failed=True; log.warning("policy embedding unavailable", exc_info=True); return None

def cosine(a,b):
    if not a or not b or len(a)!=len(b) or len(a)%4: return None
    import array
    x,y=array.array("f"),array.array("f"); x.frombytes(a); y.frombytes(b)
    nx=sum(i*i for i in x)**.5; ny=sum(i*i for i in y)**.5
    return sum(i*j for i,j in zip(x,y))/(nx*ny) if nx and ny else None

class Store:
    def __init__(self, path=None):
        home=Path(get_hermes_home()); self.path=Path(path or home/"state/policy_fallback.sqlite3")
        self.artifacts=home/"cache/policy-fallback"; self.path.parent.mkdir(parents=True,exist_ok=True); self.artifacts.mkdir(parents=True,exist_ok=True); self.init()
    def db(self):
        c=sqlite3.connect(str(self.path),timeout=15); c.row_factory=sqlite3.Row; c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON"); return c
    def init(self):
        with self.db() as d: d.executescript("""
        CREATE TABLE IF NOT EXISTS fallbacks(fallback_id TEXT PRIMARY KEY,parent_task_id TEXT NOT NULL,parent_task_hash TEXT NOT NULL,blocked_task_id TEXT NOT NULL,blocked_task_hash TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,policy_source TEXT NOT NULL,state TEXT NOT NULL,lease_owner TEXT,lease_expires_at REAL,blocked_task_summary TEXT NOT NULL,context_summary TEXT NOT NULL,completed_summary TEXT NOT NULL,source_refs TEXT NOT NULL,qwen_result TEXT,error TEXT,created_at REAL NOT NULL,started_at REAL,finished_at REAL,UNIQUE(parent_task_id,blocked_task_id,attempt));
        CREATE TABLE IF NOT EXISTS consultations(consultation_id TEXT PRIMARY KEY,fallback_id TEXT NOT NULL REFERENCES fallbacks(fallback_id) ON DELETE CASCADE,sequence INTEGER NOT NULL,reason TEXT NOT NULL,normalized_question_hash TEXT NOT NULL,question_hash TEXT NOT NULL,evidence_hash TEXT NOT NULL,embedding_model TEXT,embedding_vector BLOB,what_changed_summary TEXT NOT NULL,state TEXT NOT NULL,confidence REAL,verification_required INTEGER NOT NULL DEFAULT 1,verification_status TEXT NOT NULL DEFAULT 'not_run',verification_summary TEXT NOT NULL DEFAULT '',answer_summary TEXT NOT NULL DEFAULT '',answer_artifact_ref TEXT,answer_hash TEXT,created_at REAL NOT NULL,finished_at REAL,UNIQUE(fallback_id,sequence),UNIQUE(fallback_id,question_hash));
        CREATE INDEX IF NOT EXISTS pf_state ON fallbacks(state,created_at); CREATE INDEX IF NOT EXISTS pc_parent ON consultations(fallback_id,sequence);
        """)
    def create(self,parent_id,goal,blocked,source,reason,completed,sources):
        cleanup(self); bh=sha(normalize_question(blocked)); bid="blocked-"+bh[:20]; now=time.time()
        with self.db() as d:
            d.execute("BEGIN IMMEDIATE"); old=d.execute("SELECT * FROM fallbacks WHERE parent_task_id=? AND blocked_task_id=? AND attempt=1",(parent_id,bid)).fetchone()
            if old:return dict(old)
            fid="pf-"+uuid.uuid4().hex
            d.execute("INSERT INTO fallbacks VALUES(?,?,?,?,?,1,?,'pending',NULL,NULL,?,?,?,?,NULL,NULL,?,NULL,NULL)",(fid,parent_id,sha(goal),bid,bh,source,clip(blocked),clip(reason),clip(completed),clip(sources),now))
            return dict(d.execute("SELECT * FROM fallbacks WHERE fallback_id=?",(fid,)).fetchone())
    def claim(self,fid,owner):
        now=time.time()
        with self.db() as d:return d.execute("UPDATE fallbacks SET state='running',lease_owner=?,lease_expires_at=?,started_at=? WHERE fallback_id=? AND state='pending'",(owner,now+LEASE_TTL,now,fid)).rowcount==1
    def finish(self,fid,state,result=None,error=""):
        assert state in TERMINAL
        with self.db() as d:d.execute("UPDATE fallbacks SET state=?,qwen_result=?,error=?,finished_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE fallback_id=?",(state,clip(result,RESULT_MAX) if result is not None else None,clip(error,4096),time.time(),fid))
    def fail_stale(self):
        now=time.time()
        with self.db() as d:return d.execute("UPDATE fallbacks SET state='failed',error='worker_lost',finished_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE state='running' AND lease_expires_at<?",(now,now)).rowcount
    def list_consultations(self,fid):
        with self.db() as d:return [dict(r) for r in d.execute("SELECT * FROM consultations WHERE fallback_id=? ORDER BY sequence",(fid,))]
    def reserve_consultation(self,fid,reason,question,evidence,changed):
        cleanup(self); prior=self.list_consultations(fid); seq=len(prior)+1
        if seq>5:return None,"budget_exhausted"
        if seq>=4 and (reason not in {"conflicting_evidence","failed_verification","high_impact_decision"} or len((changed or "").strip())<20):return None,"consultations 4-5 require qualifying reason and specific what_changed_since_last_consultation"
        norm,nh,eh,qh=question_hashes(question,evidence); vec=embedding(norm)
        if seq>=4 and prior and prior[-1]["evidence_hash"]==eh:
            return None,"consultations 4-5 require new evidence, not only a changed explanation"
        for old in prior:
            if old["question_hash"]==qh:return None,"duplicate question and evidence"
            if old["state"]=="blocked":
                if old["normalized_question_hash"]==nh:return None,"blocked question may not be rephrased"
                sim=cosine(old["embedding_vector"],vec)
                if sim is not None and sim>=SIMILARITY_LIMIT:return None,f"blocked semantic duplicate ({sim:.3f})"
                if vec is None:return None,"embedding unavailable; blocked duplicate check fails closed"
        cid="pc-"+uuid.uuid4().hex
        with self.db() as d:d.execute("INSERT INTO consultations(consultation_id,fallback_id,sequence,reason,normalized_question_hash,question_hash,evidence_hash,embedding_model,embedding_vector,what_changed_summary,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'running',?)",(cid,fid,seq,reason,nh,qh,eh,EMBED_MODEL if vec else None,vec,clip(changed or "",2048),time.time()))
        return {"consultation_id":cid,"sequence":seq},""
    def finish_consultation(self,cid,state,answer,confidence,required):
        ref=None
        if answer:
            p=self.artifacts/(cid+".json"); t=p.with_suffix(".tmp"); t.write_text(json.dumps({"answer":answer},ensure_ascii=False),encoding="utf-8"); os.replace(t,p); ref=str(p)
        summary=clip(re.sub(r"\s+"," ",answer).strip(),2048)
        with self.db() as d:d.execute("UPDATE consultations SET state=?,confidence=?,verification_required=?,answer_summary=?,answer_artifact_ref=?,answer_hash=?,finished_at=? WHERE consultation_id=?",(state,confidence,int(required),summary,ref,sha(answer) if answer else None,time.time(),cid))
    def verify(self,cid,status,summary,evidence):
        if status not in {"passed","failed","inconclusive"}:raise ValueError("invalid verification status")
        refs=[str(x).strip() for x in evidence or [] if str(x).strip()]
        if status=="passed" and not refs:raise ValueError("passed requires tool-result/artifact evidence")
        text=clip(summary+("\nEvidence: "+"; ".join(refs) if refs else ""),4096)
        with self.db() as d:
            if not d.execute("SELECT 1 FROM consultations WHERE consultation_id=?",(cid,)).fetchone():raise KeyError("unknown consultation_id")
            d.execute("UPDATE consultations SET verification_status=?,verification_summary=? WHERE consultation_id=?",(status,text,cid))
        return {"consultation_id":cid,"verification_result":{"status":status,"summary":text}}
    def cleanup(self,now=None,batch=100):
        now=now or time.time(); files=bytes_=rows=0
        with self.db() as d:
            live={r[0] for r in d.execute("SELECT answer_artifact_ref FROM consultations WHERE answer_artifact_ref IS NOT NULL")}
            for cid,ref in d.execute("SELECT consultation_id,answer_artifact_ref FROM consultations WHERE answer_artifact_ref IS NOT NULL AND finished_at<? LIMIT ?",(now-ARTIFACT_TTL,batch)):
                try:p=Path(ref); size=p.stat().st_size if p.exists() else 0;p.unlink(missing_ok=True);files+=1;bytes_+=size
                except OSError:log.warning("artifact cleanup failed id=%s",cid)
                d.execute("UPDATE consultations SET answer_artifact_ref=NULL WHERE consultation_id=?",(cid,))
            for p in list(self.artifacts.glob("pc-*"))[:batch]:
                if str(p) not in live and p.stat().st_mtime<now-3600:bytes_+=p.stat().st_size;p.unlink(missing_ok=True);files+=1
            ids=d.execute("SELECT fallback_id FROM fallbacks WHERE state IN ('completed','partial','failed','timeout') AND finished_at<? LIMIT ?",(now-ROW_TTL,batch)).fetchall()
            for r in ids:d.execute("DELETE FROM fallbacks WHERE fallback_id=?",(r[0],))
            rows=len(ids);d.execute("PRAGMA wal_checkpoint(PASSIVE)");d.execute("PRAGMA incremental_vacuum(64)")
        return {"files":files,"bytes":bytes_,"rows":rows}

def cleanup(store=None,force=False):
    global _last_cleanup
    now=time.time()
    if not force and now-_last_cleanup<3600:return None
    if not _cleanup_guard.acquire(False):return None
    lock_handle = None
    try:
        target = store or Store()
        # Cross-process guard: gateway, CLI and opportunistic inserts may all
        # reach cleanup concurrently.
        import fcntl
        lock_path = Path(get_hermes_home()) / "state" / "policy_fallback.cleanup.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(lock_path, "a+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        out=target.cleanup(now);_last_cleanup=now
        if out and any(out.values()):log.info("policy cleanup files=%s bytes=%s rows=%s",out["files"],out["bytes"],out["rows"])
        return out
    finally:
        if lock_handle is not None:
            try: lock_handle.close()
            except OSError: pass
        _cleanup_guard.release()

def runtime():return _runtime.get()
@contextlib.contextmanager
def runtime_scope(value):
    tok=_runtime.set(value)
    try:yield
    finally:_runtime.reset(tok)

def parse_json(text):
    s=str(text or "").strip(); m=re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```",s,re.S|re.I); s=m.group(1) if m else s
    try:v=json.loads(s);return v if isinstance(v,dict) else None
    except Exception:return None

def classify(result,goal):
    err=str(result.get("error") or "")
    if err.startswith("content_policy_blocked:"):return {"source":"provider_policy_block","blocked":goal,"completed":[],"reason":err.split(":",1)[1].strip(),"sources":[]}
    r=parse_json(result.get("final_response"))
    if not r or r.get("status") not in {"blocked","partial"}:return None
    reason=str(r.get("reason") or r.get("blocked_reason") or "");code=str(r.get("reason_code") or r.get("category") or "")
    if "policy" not in (reason+code).casefold():return None
    left=r.get("remaining") or r.get("blocked") or []; blocked="\n".join(map(str,left)) if isinstance(left,list) else str(left)
    return {"source":"model_reported_policy","blocked":blocked,"completed":r.get("completed") or [],"reason":reason,"sources":r.get("sources") or []} if blocked.strip() else None

def maybe_run(agent,result,user_message,task_id):
    if not isinstance(result,dict) or getattr(agent,"platform","")=="subagent" or getattr(agent,"_policy_fallback_in_progress",False):return result
    goal=user_message if isinstance(user_message,str) else clip(user_message); p=classify(result,goal)
    if not p:return result
    store=Store();store.fail_stale();row=store.create(task_id,goal,p["blocked"],p["source"],p["reason"],p["completed"],p["sources"])
    if row["state"] in TERMINAL or not store.claim(row["fallback_id"],f"{os.getpid()}:{uuid.uuid4().hex[:8]}"):return result
    log.info("policy_fallback state=running fallback_id=%s parent_task_id=%s blocked_task_id=%s policy_source=%s reason_hash=%s",row["fallback_id"],task_id,row["blocked_task_id"],p["source"],sha(p["reason"])[:16])
    ctx={"fallback_id":row["fallback_id"],"blocked_task_id":row["blocked_task_id"],"parent_task_id":task_id,"parent_agent":agent,"store":store}
    child_goal=f'''Complete only this policy-blocked subtask independently:\n{p["blocked"]}\nReturn ONLY JSON: {{"status":"completed|partial|failed","source":"qwen_subagent","original_blocked_task":"...","completed":[],"remaining":[],"result":"...","notes":[]}}. Use ask_gpt only for a narrow uncertainty after your research. Verify mandatory advice and record it.'''
    child_context=clip({"original_goal":goal,"policy_source":p["source"],"policy_reason":p["reason"],"already_completed":p["completed"],"sources":p["sources"]})
    try:
        from tools.delegate_tool import delegate_task
        agent._policy_fallback_in_progress=True;agent._policy_fallback_context=ctx
        envelope=json.loads(delegate_task(goal=child_goal,context=child_context,parent_agent=agent));entries=envelope.get("results") or []; q=parse_json(entries[0].get("summary") if entries else "")
        if not q:
            store.finish(row["fallback_id"],"failed",error="invalid_qwen_json");log.warning("policy_fallback state=failed fallback_id=%s error=invalid_qwen_json",row["fallback_id"])
            out=dict(result);out["policy_fallback_id"]=row["fallback_id"];out["remaining"]=[p["blocked"]];return out
        state=q.get("status") if q.get("status") in {"completed","partial","failed"} else "failed"; consult=store.list_consultations(row["fallback_id"])
        if state != "completed" and not (q.get("remaining") or []):q["remaining"]=[p["blocked"]]
        if state=="completed" and any(c["verification_required"] and c["verification_status"]=="not_run" for c in consult):state="partial";q.setdefault("remaining",[]).append("mandatory GPT consultation verification not run")
        q["consultations"]=[{"consultation_id":c["consultation_id"],"state":c["state"],"confidence":c["confidence"],"verification_required":bool(c["verification_required"]),"verification_result":{"status":c["verification_status"],"summary":c["verification_summary"]}} for c in consult]
        store.finish(row["fallback_id"],state,q);log.info("policy_fallback state=%s fallback_id=%s consultations=%d remaining=%d",state,row["fallback_id"],len(consult),len(q.get("remaining") or []));merged={"status":"completed" if state=="completed" else "partial","source":"gpt_qwen_policy_fallback","policy_source":p["source"],"gpt_completed":p["completed"],"qwen":q,"remaining":q.get("remaining") or []}
        out=dict(result);out.update(final_response=json.dumps(merged,ensure_ascii=False),completed=state=="completed",failed=state=="failed",policy_fallback_id=row["fallback_id"]);out.pop("error",None);return out
    except TimeoutError as e:store.finish(row["fallback_id"],"timeout",error=str(e));return result
    except Exception as e:log.exception("policy fallback failed id=%s",row["fallback_id"]);store.finish(row["fallback_id"],"failed",error=str(e));return result
    finally:agent._policy_fallback_in_progress=False;agent._policy_fallback_context=None
