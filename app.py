"""BlackLine Solutions: Meals & Entertainment Guidance (Streamlit)."""

from __future__ import annotations

import hmac
import html
import io
import re
from datetime import date

import anthropic
import pypdfium2 as pdfium
import streamlit as st

import engine
import report

st.set_page_config(page_title="Meals & Entertainment Guidance", page_icon="🧾", layout="centered")

MODEL = st.secrets.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
SEARCH_TOOL = {
    "type": st.secrets.get("WEB_SEARCH_TOOL_VERSION", "web_search_20250305"),
    "name": "web_search",
    "max_uses": int(st.secrets.get("WEB_SEARCH_MAX_USES", 2)),
    # Primary sources only: better answers, and fewer, shorter pages pulled into the request.
    "allowed_domains": ["irs.gov", "law.cornell.edu", "ecfr.gov", "congress.gov", "federalregister.gov", "govinfo.gov"],
    "user_location": {"type": "approximate", "country": "US", "region": "North Carolina", "timezone": "America/New_York"},
}

st.markdown(
    """
<style>
/* tighter page */
.block-container {max-width: 780px; padding-top: 3.6rem; padding-bottom: 2rem;}
[data-testid="stVerticalBlock"] {gap: 0.55rem;}
h1.app-title {font-size: 1.45rem; margin: 0 0 0.1rem 0; padding: 0;}
/* compact info box */
[data-testid="stAlert"] {padding: 0.15rem 0;}
[data-testid="stAlert"] p {font-size: 0.85rem; line-height: 1.35;}
/* compact uploader: small drop strip + button */
[data-testid="stFileUploaderDropzone"] {padding: 0.35rem 0.6rem; min-height: 0;}
[data-testid="stFileUploaderDropzoneInstructions"] svg {display: none;}
[data-testid="stFileUploaderDropzoneInstructions"] small {display: none;}
[data-testid="stFileUploaderDropzoneInstructions"] span {font-size: 0.85rem;}
/* result cards */
.result-card {border-radius: 10px; padding: 12px 14px; margin: 4px 0; border: 1px solid;}
.pct-0   {background: #fcebeb; border-color: #b3261e;}
.pct-50  {background: #e1f5ee; border-color: #0f6e56;}
.pct-100 {background: #eaf3de; border-color: #27500a;}
.np      {background: #e8eef8; border-color: #1d5fa8;}
.ask     {background: #fde3d3; border-color: #8a4315;}
.result-card .head {font-weight: 700; font-size: 1.05rem; margin-bottom: 2px;}
.result-card .cat {font-weight: 600; margin-bottom: 4px;}
.result-card, .result-card * {color: #1f2937;}
</style>
""",
    unsafe_allow_html=True,
)


# ---------- optional password gate ----------
def gate() -> None:
    pw = st.secrets.get("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.markdown('<h1 class="app-title">🧾 Meals &amp; Entertainment Guidance</h1>', unsafe_allow_html=True)
    st.caption("BlackLine Solutions, LLC · team tool")
    with st.form("login", border=True):
        st.markdown("Enter the team password to access this app.")
        entered = st.text_input("Team password", type="password", label_visibility="collapsed",
                                placeholder="Password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        if hmac.compare_digest(entered, pw):
            st.session_state.authed = True
            st.rerun()
        st.error("Incorrect password. Ask Hardy if you need it.")
    st.stop()


gate()

# ---------- state ----------
ss = st.session_state
ss.setdefault("messages", [])      # API conversation for the current expense
ss.setdefault("tool_id", None)     # pending clarifying-question tool call
ss.setdefault("result", None)
ss.setdefault("harvested", [])
ss.setdefault("form_id", 0)        # bumped to clear inputs after a final answer
ss.setdefault("qa", [])            # clarifying questions and answers, for the PDF


@st.cache_resource
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])


def call(new_message: dict) -> bool:
    msgs = ss.messages + [new_message]
    try:
        with st.spinner("Reviewing the firm guides and checking current IRS guidance…"):
            result, tool_id, harvested, usage = engine.run(client(), MODEL, msgs, SEARCH_TOOL)
    except (engine.GuidanceError, anthropic.APIError) as e:
        st.error(f"Something went wrong: {e}")
        return False
    ss.messages, ss.tool_id, ss.result = msgs, tool_id, result
    ss.harvested = harvested
    tot = ss.get("usage") if new_message.get("content", [{}])[0].get("type") == "tool_result" else None
    ss.usage = {k: usage[k] + (tot or {}).get(k, 0) for k in usage}
    if result.get("status") == "final":
        ss.messages, ss.tool_id = [], None   # next submission starts fresh
        ss.form_id += 1                       # clears description + upload
    return True


@st.cache_data
def guide_pages(path_str: str) -> list[bytes]:
    """Render each PDF page to a PNG so the guide can be viewed in the app."""
    pdf = pdfium.PdfDocument(path_str)
    pages = []
    for page in pdf:
        img = page.render(scale=2).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pages.append(buf.getvalue())
    pdf.close()
    return pages


@st.dialog("Firm guide", width="large")
def show_guide(idx: int) -> None:
    title, path = engine.GUIDES[idx]
    st.markdown(f"**{title}**")
    st.download_button("Download PDF", path.read_bytes(), file_name=path.name, mime="application/pdf")
    for png in guide_pages(str(path)):
        st.image(png, width="stretch")


# ---------- header row: title + guide buttons ----------
t1, t2, t3 = st.columns([2.6, 1, 1], vertical_alignment="center")
t1.markdown('<h1 class="app-title">🧾 Meals &amp; Entertainment Guidance</h1>', unsafe_allow_html=True)
if t2.button("📘 General guide", width="stretch"):
    show_guide(0)
if t3.button("📘 Nonprofit guide", width="stretch"):
    show_guide(1)

entity = st.radio("Client type", ["Business (taxable)", "Nonprofit"], horizontal=True, key="entity",
                  label_visibility="collapsed")

fid = ss.form_id
st.info(
    "Describe the expense and optionally attach the receipt or invoice. Claude checks it against the firm's "
    "M&E guides and searches current IRS guidance, and flags any conflict between the two. Uploads are sent "
    "to Anthropic for processing; this app saves nothing. Bookkeeping guidance, not tax or legal advice.",
    icon="ℹ️",
)
desc = st.text_area(
    "Describe the expense",
    placeholder="e.g. Took a prospective client to dinner to discuss a new engagement",
    key=f"desc_{fid}",
    height=90,
    label_visibility="collapsed",
)

u1, u2 = st.columns([3, 1], vertical_alignment="center")
with u1:
    upload = st.file_uploader(
        "Receipt or invoice (optional)",
        type=["pdf", "png", "jpg", "jpeg", "webp", "gif"],
        key=f"file_{fid}",
        label_visibility="collapsed",
    )
with u2:
    submit = st.button("Submit", type="primary", width="stretch")

if submit:
    if not desc.strip() and not upload:
        st.warning("Describe the expense or attach a receipt/invoice.")
    else:
        att = None
        if upload:
            try:
                att = engine.attachment_block(upload.getvalue(), upload.type, upload.name)
            except Exception as e:
                st.error(str(e))
                st.stop()
        ss.messages, ss.tool_id, ss.result, ss.qa = [], None, None, []
        ss.last_entity, ss.last_desc = entity, desc.strip()
        ss.last_file = upload.name if upload else None
        if call(engine.first_user_message(desc, entity, att)):
            st.rerun()

# ---------- result ----------
r = ss.result
if r:
    entity_used = ss.get("last_entity", "Business (taxable)")
    if r.get("status") == "needs_info":
        questions = r.get("questions") or ([r["question"]] if r.get("question") else [])
        q_items = "".join(f"<li>{html.escape(q)}</li>" for q in questions)
        st.markdown(
            f'<div class="result-card ask"><div class="head">Need a bit more info</div>'
            f'<div>{html.escape(r.get("reasoning", ""))}</div>'
            f'<ol style="margin:6px 0 0 0;font-weight:600">{q_items}</ol></div>',
            unsafe_allow_html=True,
        )
        with st.form("reply", clear_on_submit=True, border=False):
            label = "Your answer" if len(questions) <= 1 else "Your answers (answer each question)"
            reply = st.text_area(label, height=80 if len(questions) <= 1 else 110)
            if st.form_submit_button("Reply", type="primary") and reply.strip():
                ss.qa.append({"questions": questions, "answer": reply.strip()})
                if call(engine.reply_message(ss.tool_id, reply)):
                    st.rerun()
    else:
        lines = r.get("line_items") or []
        acct_note = ('<div style="font-size:0.8rem;margin-top:6px;opacity:0.75">Post to the client\'s own account '
                     'that carries each treatment. Expense types are descriptions, not account names.</div>')
        if lines:
            rows = ""
            for s in report.summarize(lines):
                ded = f" · deductible {report.money(s['deductible'])}" if s["deductible"] is not None else ""
                n = s["count"]
                rows += (f"<div><b>{html.escape(s['treatment'])}</b>: {report.money(s['amount'])} "
                         f"({n} {'charge' if n == 1 else 'charges'}){ded}</div>")
            st.markdown(
                f'<div class="result-card np"><div class="head">Mixed treatment: see line items</div>'
                f'<div style="margin-bottom:6px">{html.escape(r.get("reasoning", ""))}</div>{rows}'
                f'<div style="margin-top:4px"><b>Total charges reviewed: {report.money(report.total_charges(lines))}</b> '
                f'<span style="opacity:0.75">(excludes payments, credits, and balance forward)</span></div>{acct_note}</div>',
                unsafe_allow_html=True,
            )
            st.dataframe(
                [{"Date": ln.get("date") or "", "Charge": ln.get("description", ""),
                  "Amount": float(ln.get("amount") or 0), "Treatment": ln.get("treatment", ""),
                  "Expense type": ln.get("category") or "", "Note": ln.get("note") or ""} for ln in lines],
                hide_index=True, width="stretch",
                column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
            )
        else:
            pct = r.get("deductible_percent")
            if pct is not None:
                css, head = f"pct-{pct}", f"{pct}% deductible"
                if r.get("functional_class"):
                    head += f" · {r['functional_class']}"
            else:
                css, head = "np", r.get("functional_class") or "Functional class"
            cat = (f'<div class="cat">Expense type: {html.escape(r["category"])}</div>'
                   if r.get("category") else "")
            st.markdown(
                f'<div class="result-card {css}"><div class="head">{html.escape(head)}</div>{cat}'
                f'<div>{html.escape(r.get("reasoning", ""))}</div>{acct_note}</div>',
                unsafe_allow_html=True,
            )

        wc = r.get("web_check", {})
        discrepancies = r.get("discrepancies") or []
        if discrepancies or wc.get("result") == "discrepancy":
            st.error("**⚠️ Discrepancy: current sources differ from the firm guide.** The answer above follows "
                     "the guide. Review before coding, and flag to Hardy so the guide can be updated.")
            for d in discrepancies:
                st.markdown(
                    f"**{d['topic']}**  \n"
                    f"• **Guide says:** {d['guide_says']}  \n"
                    f"• **Current sources say:** {d['web_says']}  \n"
                    f"• **Treatment per current sources:** {d['treatment_per_web']}"
                )
        elif wc.get("result") == "agrees":
            st.success(f"✅ Web check agrees with the firm guide. {wc.get('summary', '')}")
        else:
            st.warning(f"Web check inconclusive. {wc.get('summary', '')}")

        if r.get("receipt_summary") and ss.get("last_file"):
            st.caption(f"**From the attachment:** {r['receipt_summary']}")
        if r.get("caveats"):
            st.caption(f"**Note:** {r['caveats']}")

        sources = r.get("sources") or ss.harvested[:6]
        if sources and not r.get("sources"):
            r = {**r, "sources": sources}
        if sources:
            with st.expander("Sources checked"):
                for s in sources:
                    st.markdown(f"- [{s['title']}]({s['url']})")

        if not ss.get("last_file"):
            r = {**r, "receipt_summary": None}   # never show attachment details when nothing was attached
        pdf_bytes = report.build_pdf(r, entity_used, ss.get("last_desc", ""), ss.get("last_file"), ss.get("qa", []))
        slug = re.sub(r"[^A-Za-z0-9]+", "-", r.get("category") or ("Line-Items" if r.get("line_items") else "result")).strip("-")
        st.download_button("📄 Save this answer as PDF", pdf_bytes,
                           file_name=f"ME-Guidance_{slug}_{date.today():%Y-%m-%d}.pdf", mime="application/pdf")

    if ss.get("usage") and str(st.secrets.get("SHOW_USAGE", "true")).lower() == "true":
        u = ss.usage
        st.caption(f"Usage: {u['input'] + u['cache_read'] + u['cache_write']:,} input tokens "
                   f"({u['cache_read']:,} from cache) · {u['output']:,} output · {u['searches']} web searches · "
                   f"{u['calls']} API calls")
