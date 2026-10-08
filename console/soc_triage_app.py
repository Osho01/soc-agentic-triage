"""
SOC Alert Triage Console
Streamlit front end for the governed multi-agent triage workflow in n8n.

Run:   streamlit run soc_triage_app.py
Needs: soc_alert_corpus.json in the same folder, n8n running with the
       'SOC Triage - Full Agent Chain v6 (UI)' workflow published.
"""
import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import streamlit as st

HERE = Path(__file__).parent
CORPUS_PATH = HERE / "soc_alert_corpus.json"
DEFAULT_WEBHOOK = "https://n8n-production-c001.up.railway.app/webhook/soc-triage"

DISPOSITIONS = {
    "A": ("Closed automatically", "Every guardrail passed, so the agents closed this alert and logged why.", "#2E6A4F"),
    "H": ("Needs your approval", "Nothing happens until an analyst approves, rejects or overrides.", "#94620A"),
    "E": ("Escalated to IR lead", "The on-call IR lead has been paged. Containment waits for approval.", "#9E2A2B"),
}
SEVERITY_WORDS = {0: "Informational", 1: "Low", 2: "Medium", 3: "High", 4: "Critical"}

st.set_page_config(page_title="SOC alert triage", page_icon="🛡️", layout="wide")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton button, .stSelectbox, .stTextInput input {
  font-family: 'IBM Plex Sans', system-ui, sans-serif;
}
code, pre { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
.block-container { padding-top: 2rem; max-width: 1200px; }
h1 { font-weight: 600; letter-spacing: -0.01em; margin-bottom: 0.1rem; }
.lede { color: inherit; opacity: 0.72; margin-bottom: 1.5rem; }

.verdict { display: flex; align-items: stretch; border: 1px solid #D5DBE1; border-radius: 6px;
           overflow: hidden; margin: 0.5rem 0 1.25rem 0; background: #FFFFFF; }
.verdict .letter { color: #FFFFFF; font-size: 3.25rem; font-weight: 700; line-height: 1;
                   display: flex; align-items: center; justify-content: center; min-width: 6rem; }
.verdict .body { padding: 0.9rem 1.2rem; }
.verdict .title { font-size: 1.35rem; font-weight: 600; color: #1F2A33; }
.verdict .explain { color: #4A5661; margin-top: 0.15rem; }
.verdict .facts { margin-top: 0.6rem; display: flex; gap: 1.5rem; flex-wrap: wrap; color: #1F2A33; }
.verdict .facts span b { font-weight: 600; }

.notice { border-left: 4px solid; padding: 0.6rem 0.9rem; margin: 0 0 0.75rem 0; background: #F6F7F9; color: #1F2A33; }
.notice.warn { border-color: #9E2A2B; }
.notice.info { border-color: #24568C; }
.headline { font-size: 1.2rem; font-weight: 600; color: inherit; margin-bottom: 0.5rem; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- helpers
@st.cache_data
def load_corpus():
    if not CORPUS_PATH.exists():
        return None
    return json.loads(CORPUS_PATH.read_text())


def strip_answers(alert: dict) -> dict:
    """The agents must never see the gold standard."""
    return {k: v for k, v in alert.items() if k not in ("gold_standard", "category")}


def approval_url(resume_url: str, **params) -> str:
    sep = "&" if "?" in resume_url else "?"
    return resume_url + sep + "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())


def log(event: str, alert_id: str, **extra):
    st.session_state.audit.append({
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "event": event, "alert_id": alert_id,
        "analyst": st.session_state.get("analyst", ""), **extra,
    })


def explain_http_error(url: str, err: Exception) -> str:
    if isinstance(err, requests.exceptions.ConnectionError):
        return (f"Could not reach n8n at {url}. Check that the Terminal window running "
                "`npx n8n` is still open.")
    if isinstance(err, requests.exceptions.Timeout):
        return ("n8n took longer than 3 minutes to answer. The Groq rate limit may be retrying. "
                "Check the Executions tab in n8n, then try again.")
    if isinstance(err, requests.exceptions.HTTPError) and err.response is not None:
        code = err.response.status_code
        if code == 404:
            return ("n8n answered, but no workflow is listening at this address. Publish the v6 workflow "
                    "in n8n, or, if you are testing, click 'Execute workflow' first and use the "
                    "/webhook-test/ address.")
        return f"n8n returned HTTP {code}: {err.response.text[:300]}"
    return f"Unexpected error: {err}"


# ---------------------------------------------------------------- state
st.session_state.setdefault("audit", [])
st.session_state.setdefault("result", None)
st.session_state.setdefault("decision", None)
st.session_state.setdefault("gold", None)

corpus = load_corpus()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("Connection")
    webhook = st.text_input("n8n workflow address", DEFAULT_WEBHOOK,
                            help="Published workflow: /webhook/soc-triage. Testing in the editor: /webhook-test/soc-triage")
    st.session_state.analyst = st.text_input("Your name (for the audit log)", "analyst1")
    api_key = st.text_input("Workflow key", type="password",
                            help="The X-SOC-Key value set on the webhook node in n8n.")
    st.divider()
    st.subheader("Audit log")
    if st.session_state.audit:
        st.caption(f"{len(st.session_state.audit)} events this session")
        buf = io.StringIO()
        fields = sorted({k for row in st.session_state.audit for k in row})
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        w.writerows(st.session_state.audit)
        st.download_button("Download audit log (CSV)", buf.getvalue(), "soc_triage_audit_log.csv", "text/csv")
    else:
        st.caption("Events appear here once you triage an alert.")

# ---------------------------------------------------------------- header
st.title("SOC alert triage")
st.markdown('<div class="lede">Three AI agents enrich, assess and summarise each alert. '
            'Guardrails decide what they are allowed to do alone. You decide the rest.</div>',
            unsafe_allow_html=True)

# ---------------------------------------------------------------- input
left, right = st.columns([3, 1])
with left:
    mode = st.radio("Alert source", ["Test alert from corpus", "Paste alert JSON"], horizontal=True,
                    label_visibility="collapsed")
    alert, gold = None, None
    if mode == "Test alert from corpus":
        if corpus is None:
            st.error(f"soc_alert_corpus.json not found. Put it in the same folder as this app: {HERE}")
        else:
            options = {f"{a['alert_id']}  |  {a['rule_name']}": a for a in corpus["alerts"]}
            choice = st.selectbox("Alert", list(options), label_visibility="collapsed")
            picked = options[choice]
            alert, gold = strip_answers(picked), picked.get("gold_standard")
    else:
        pasted = st.text_area("Alert JSON", height=180, placeholder='{"alert_id": "ALT-9001", "timestamp": "...", "source_tool": "...", "raw": {...}}')
        if pasted.strip():
            try:
                alert = json.loads(pasted)
            except json.JSONDecodeError as e:
                st.error(f"That isn't valid JSON: {e.msg} at line {e.lineno}, column {e.colno}.")

with right:
    st.write("")
    run = st.button("Run triage", type="primary", width="stretch", disabled=alert is None)

if alert is not None:
    with st.expander("View the alert the agents will receive"):
        st.json(alert)

if run and alert is not None:
    st.session_state.result, st.session_state.decision, st.session_state.gold = None, None, gold
    started = time.time()
    with st.status("Running the agent chain…", expanded=True) as status:
        st.write("Orchestrator checks the alert and screens it for prompt injection")
        st.write("Asset, identity and threat intel lookups")
        st.write("Enrichment, Risk and Incident Summary agents")
        try:
            r = requests.post(webhook,
                              json={"alert": alert, "analyst": st.session_state.analyst},
                              headers={"X-SOC-Key": api_key},
                              timeout=180)
            r.raise_for_status()
            result = r.json()
            if isinstance(result, list):
                result = result[0] if result else {}
            if result.get("status") == "REJECTED_INVALID_INPUT":
                status.update(label="Alert rejected before reaching the agents", state="error", expanded=True)
                errs = result.get("validation_errors") or ["no reason given"]
                st.error("The orchestrator rejected this alert, so no AI agent saw it. Fix these and resubmit: "
                         + "; ".join(errs) + ".")
                log("REJECTED_INVALID_INPUT", result.get("alert_id") or "unknown", reason="; ".join(errs))
                st.stop()
            if "case_file" not in result:
                raise ValueError("n8n answered, but not with a case file. If the alert failed validation, "
                                 f"n8n returned: {json.dumps(result)[:400]}")
            st.session_state.result = result
            st.session_state.elapsed = time.time() - started
            status.update(label=f"Done in {st.session_state.elapsed:.1f} seconds", state="complete", expanded=False)
            log("TRIAGED", result.get("alert_id", "?"), agent_disposition=result["case_file"].get("model_disposition"),
                final_disposition=result.get("final_disposition"), model=result["case_file"].get("model"))
        except (requests.exceptions.RequestException, ValueError) as err:
            status.update(label="Triage did not complete", state="error", expanded=True)
            st.error(explain_http_error(webhook, err) if isinstance(err, requests.exceptions.RequestException) else str(err))

# ---------------------------------------------------------------- result
res = st.session_state.result
if res:
    cf = res["case_file"]
    disp = res.get("final_disposition", "H")
    title, explain, colour = DISPOSITIONS.get(disp, DISPOSITIONS["H"])
    sev = cf.get("severity")
    sev_txt = f"{sev} ({SEVERITY_WORDS.get(sev, '?')})" if sev is not None else "not assessed"

    st.markdown(f"""
<div class="verdict">
  <div class="letter" style="background:{colour}">{disp}</div>
  <div class="body">
    <div class="title">{title}</div>
    <div class="explain">{explain}</div>
    <div class="facts">
      <span><b>Alert</b> {cf.get('alert_id')}</span>
      <span><b>Severity</b> {sev_txt}</span>
      <span><b>Classification</b> {cf.get('classification') or 'not assessed'}</span>
      <span><b>Confidence</b> {cf.get('confidence') or '–'}</span>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

    if cf.get("injection_detected"):
        st.markdown('<div class="notice warn"><b>Prompt injection detected.</b> Text inside this alert tried to '
                    'instruct the AI. It was treated as evidence of an attacker, not followed.</div>',
                    unsafe_allow_html=True)
    if cf.get("guardrail_overrode_model"):
        st.markdown('<div class="notice warn"><b>Guardrail override.</b> The Risk agent proposed '
                    f'<b>{cf.get("model_disposition") or "an invalid answer"}</b>; the guardrails changed it to '
                    f'<b>{disp}</b>. Reason: {"; ".join(cf.get("guardrail_violations", []))}</div>',
                    unsafe_allow_html=True)
    if cf.get("enrichment_degraded"):
        st.markdown('<div class="notice info"><b>Partial evidence.</b> The Enrichment agent failed, so this '
                    'assessment used raw tool output only.</div>', unsafe_allow_html=True)
    if cf.get("crown_jewel_involved"):
        st.markdown('<div class="notice info"><b>Crown-jewel asset involved.</b> This alert can never be '
                    'closed automatically.</div>', unsafe_allow_html=True)

    case_col, decide_col = st.columns([3, 2], gap="large")
    with case_col:
        st.markdown(f'<div class="headline">{cf.get("headline", "")}</div>', unsafe_allow_html=True)
        st.write(cf.get("summary", ""))
        if cf.get("decisive_evidence"):
            st.markdown("**What decided it**")
            st.write(cf["decisive_evidence"])
        if cf.get("what_would_change_the_verdict"):
            st.markdown("**What would change the verdict**")
            st.write(cf["what_would_change_the_verdict"])
        for w in cf.get("analyst_warnings") or []:
            st.warning(w)

    with decide_col:
        st.markdown("**Your decision**")
        resume = res.get("resume_url")
        if disp == "A":
            st.success("Closed automatically and logged. No approval needed.")
        elif st.session_state.decision:
            d = st.session_state.decision
            st.success(f"Recorded: **{d['status']}** by {d['analyst']}. Outcome applied: **{d['applied']}**.")
        elif not resume:
            st.error("This case file has no approval address. Re-import the v6 workflow in n8n.")
        else:
            actions = cf.get("recommended_actions") or []
            if actions:
                st.caption("Approving releases these actions:")
                for a in actions:
                    st.markdown(f"- {a.get('action', a) if isinstance(a, dict) else a}")

            def send(decision, **extra):
                url = approval_url(resume, decision=decision, analyst=st.session_state.analyst, **extra)
                try:
                    rr = requests.get(url, timeout=60)
                    rr.raise_for_status()
                    try:
                        body = rr.json()
                        body = body[0] if isinstance(body, list) and body else body
                    except ValueError:
                        body = {}
                    status = body.get("status", decision.upper())
                    applied = body.get("applied_outcome", extra.get("disposition", "?"))
                    st.session_state.decision = {"status": status, "applied": applied, "analyst": st.session_state.analyst}
                    log(status, cf.get("alert_id"), agent_disposition=cf.get("model_disposition"),
                        final_disposition=disp, applied_outcome=applied)
                except requests.exceptions.RequestException as err:
                    st.error(explain_http_error(url, err) + " If you already decided on this alert, the link is used up.")

            b1, b2 = st.columns(2)
            if b1.button("Approve", type="primary", width="stretch"):
                send("approve"); st.rerun()
            if b2.button("Reject", width="stretch"):
                send("reject"); st.rerun()
            with st.expander("Override the disposition"):
                new = st.radio("Change to", ["A", "H", "E"], horizontal=True, index=2,
                               format_func=lambda k: f"{k}: {DISPOSITIONS[k][0]}")
                if st.button("Apply override", width="stretch"):
                    send("override", disposition=new); st.rerun()

    t1, t2, t3, t4, t5 = st.tabs(["Recommended actions", "Evidence gaps and MITRE", "Agent performance",
                                  "Check against gold standard", "Full case file"])
    with t1:
        acts = cf.get("recommended_actions") or []
        if acts:
            st.dataframe([{"Action": a.get("action"), "Needs approval": a.get("requires_approval"),
                           "Why": a.get("rationale")} for a in acts if isinstance(a, dict)],
                         width="stretch", hide_index=True)
        else:
            st.caption("The Risk agent recommended no actions.")
    with t2:
        gaps = cf.get("evidence_gaps") or []
        st.markdown("**Missing evidence that would change the assessment**")
        st.write("\n".join(f"- {g}" for g in gaps) if gaps else "None reported.")
        mitre = cf.get("mitre_techniques") or []
        st.markdown("**MITRE ATT&CK techniques**")
        st.write("\n".join(f"- {m.get('id')}: {m.get('name')}" if isinstance(m, dict) else f"- {m}" for m in mitre)
                 if mitre else "None mapped.")
    with t3:
        m = res.get("metrics", {})
        rows = [{"Agent": k.capitalize(), "Latency (s)": v.get("latency_s"), "Prompt tokens": v.get("prompt_tokens"),
                 "Output tokens": v.get("completion_tokens"), "Reasoning tokens": v.get("reasoning_tokens"),
                 "Finished": v.get("finish_reason")} for k, v in m.get("per_agent", {}).items()]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        tot = m.get("totals", {})
        c1, c2, c3 = st.columns(3)
        c1.metric("Model time", f"{tot.get('latency_s', 0)} s")
        c2.metric("End-to-end time", f"{st.session_state.get('elapsed', 0):.1f} s")
        c3.metric("Total tokens", (tot.get("prompt_tokens") or 0) + (tot.get("completion_tokens") or 0))
        st.caption(f"Model: {cf.get('model')}")
    with t4:
        g = st.session_state.gold
        if not g:
            st.caption("Only available for test alerts from the corpus.")
        else:
            model_d = cf.get("model_disposition")
            st.dataframe([
                {"Field": "Severity", "Gold standard": str(g["severity"]), "Agents": str(sev), "Match": sev == g["severity"]},
                {"Field": "Classification", "Gold standard": g["classification"], "Agents": cf.get("classification"),
                 "Match": bool(cf.get("classification")) and g["classification"].startswith(cf["classification"])},
                {"Field": "Disposition (agent)", "Gold standard": g["disposition"], "Agents": model_d, "Match": model_d == g["disposition"]},
                {"Field": "Disposition (after guardrails)", "Gold standard": g["disposition"], "Agents": disp, "Match": disp == g["disposition"]},
            ], width="stretch", hide_index=True)
            if disp == "A" and g["disposition"] != "A":
                st.error("Unsafe outcome: auto-closed an alert that needed a human.")
            else:
                st.success("No unsafe outcome. Nothing was auto-closed that needed a human.")
            with st.expander("Gold-standard reasoning"):
                st.write(g.get("justification", ""))
    with t5:
        st.json(res)
