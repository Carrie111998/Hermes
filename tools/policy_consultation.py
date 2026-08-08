"""Host-owned consultation tools exposed only to policy-fallback workers."""
import json, logging
from agent.policy_fallback import runtime
from tools.registry import registry, tool_error

log=logging.getLogger(__name__)
REASONS={"low_confidence","conflicting_evidence","failed_verification","high_impact_decision"}

def available():
    # Availability is decided by the internal toolset, which is attached while
    # the child is constructed (before its runtime ContextVar is bound).
    # Handlers still enforce the runtime capability token on every call.
    return True

def _content(response):
    choice=response.choices[0]; msg=choice.message
    value=getattr(msg,"content","") or ""
    if isinstance(value,list):
        value="".join(str(getattr(p,"text",None) or (p.get("text","") if isinstance(p,dict) else "")) for p in value)
    return str(value)

def ask_gpt(question,context,evidence,reason,what_changed_since_last_consultation=None):
    rt=runtime()
    if not rt:return tool_error("ask_gpt is available only inside a policy-fallback Qwen worker")
    question=str(question or "").strip(); context=str(context or "").strip(); evidence=evidence or []
    if not question:return tool_error("question is required")
    if reason not in REASONS:return tool_error("invalid consultation reason")
    if not isinstance(evidence,list):return tool_error("evidence must be an array")
    store=rt["store"]
    reserved,error=store.reserve_consultation(rt["fallback_id"],reason,question,evidence,what_changed_since_last_consultation or "")
    if not reserved:return json.dumps({"status":"budget_exhausted" if error=="budget_exhausted" else "failed","error":error},ensure_ascii=False)
    cid=reserved["consultation_id"]; parent=rt["parent_agent"]
    log.info("policy_consultation state=running fallback_id=%s consultation_id=%s sequence=%s reason=%s",rt["fallback_id"],cid,reserved["sequence"],reason)
    bounded_evidence=[]
    for item in evidence[:8]:
        if isinstance(item,dict):
            bounded_evidence.append({"source":str(item.get("source") or "")[:512],"excerpt":str(item.get("excerpt") or "")[:1024]})
    prompt={"question":question[:2048],"context":context[:4096],"evidence":bounded_evidence,"reason":reason}
    system="""You are a stateless technical consultant. Answer only the narrow question using the supplied facts. Do not call tools, delegate, or propose actions outside the question. Return ONLY JSON: {"status":"answered","answer":"...","confidence":0.0,"verification_required":true,"caveats":[],"suggested_checks":[]}. Confidence is only your subjective signal. Separate facts from assumptions."""
    try:
        kwargs={"model":parent.model,"messages":[{"role":"system","content":system},{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}],"max_tokens":2048}
        response=parent.client.chat.completions.create(**kwargs)
        raw=_content(response); data=json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        if not isinstance(data,dict):raise ValueError("consultation response is not an object")
        status=str(data.get("status") or "answered")
        if status=="blocked":
            store.finish_consultation(cid,"blocked",str(data.get("answer") or data.get("reason") or "provider/model blocked consultation"),None,True)
            log.warning("policy_consultation state=blocked fallback_id=%s consultation_id=%s",rt["fallback_id"],cid)
            return json.dumps({"consultation_id":cid,"status":"blocked","reason":str(data.get("reason") or "policy block"),"verification_result":{"status":"not_run","summary":""}},ensure_ascii=False)
        confidence=data.get("confidence")
        try:confidence=max(0.0,min(1.0,float(confidence)))
        except Exception:confidence=None
        required=bool(data.get("verification_required")) or confidence is None or confidence<.75 or reason in {"high_impact_decision","failed_verification","conflicting_evidence"}
        answer=str(data.get("answer") or "")
        store.finish_consultation(cid,"answered",answer,confidence,required)
        log.info("policy_consultation state=answered fallback_id=%s consultation_id=%s confidence=%s verification_required=%s",rt["fallback_id"],cid,confidence,required)
        return json.dumps({"consultation_id":cid,"status":"answered","answer":answer,"confidence":confidence,"verification_required":required,"caveats":data.get("caveats") or [],"suggested_checks":data.get("suggested_checks") or [],"verification_result":{"status":"not_run","summary":""}},ensure_ascii=False)
    except Exception as exc:
        text=str(exc)
        blocked="policy" in text.casefold() or "safety" in text.casefold()
        state="blocked" if blocked else "failed"
        store.finish_consultation(cid,state,text,None,True)
        return json.dumps({"consultation_id":cid,"status":state,"reason":text[:1000],"verification_result":{"status":"not_run","summary":""}},ensure_ascii=False)

def record_gpt_verification(consultation_id,status,summary,evidence):
    rt=runtime()
    if not rt:return tool_error("record_gpt_verification is available only inside policy fallback")
    try:return json.dumps(rt["store"].verify(str(consultation_id),str(status),str(summary),evidence or []),ensure_ascii=False)
    except Exception as exc:return tool_error(str(exc))

registry.register(name="ask_gpt",toolset="policy_consultation",check_fn=available,emoji="🧭",
 schema={"name":"ask_gpt","description":"Ask the parent GPT one narrow technical question after independent research. Never retry a blocked consultation.","parameters":{"type":"object","properties":{"question":{"type":"string"},"context":{"type":"string"},"evidence":{"type":"array","items":{"type":"object","properties":{"source":{"type":"string"},"excerpt":{"type":"string"}},"required":["source","excerpt"]}},"reason":{"type":"string","enum":sorted(REASONS)},"what_changed_since_last_consultation":{"type":["string","null"]}},"required":["question","context","evidence","reason"]}},
 handler=lambda a,**kw:ask_gpt(a.get("question"),a.get("context"),a.get("evidence"),a.get("reason"),a.get("what_changed_since_last_consultation")))
registry.register(name="record_gpt_verification",toolset="policy_consultation",check_fn=available,emoji="✅",
 schema={"name":"record_gpt_verification","description":"Record an instrument-backed verification of a GPT consultation.","parameters":{"type":"object","properties":{"consultation_id":{"type":"string"},"status":{"type":"string","enum":["passed","failed","inconclusive"]},"summary":{"type":"string"},"evidence":{"type":"array","items":{"type":"string"}}},"required":["consultation_id","status","summary","evidence"]}},
 handler=lambda a,**kw:record_gpt_verification(a.get("consultation_id"),a.get("status"),a.get("summary"),a.get("evidence")))
