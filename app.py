"""BlackLine Solutions: Meals & Entertainment Guidance (Streamlit)."""

from __future__ import annotations

import hmac
import html
import io

import anthropic
import pypdfium2 as pdfium
import streamlit as st

import engine

st.set_page_config(page_title="Meals & Entertainment Guidance", page_icon="🧾", layout="centered")

MODEL = st.secrets.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
SEARCH_TOOL = {
    "type": st.secrets.get("WEB_SEARCH_TOOL_VERSION", "web_search_20250305"),
    "name": "web_search",
    "max_uses": int(st.secrets.get("WEB_SEARCH_MAX_USES", 4)),
    "user_location": {"type": "approximate", "country": "US", "region": "North Carolina", "timezone": "America/New_York"},
}

st.markdown(
    """
<style>
.block-container {max-width: 780px; padding-top: 2rem;}
.result-card {border-radius: 10px; padding: 14px 16px; margin: 10px 0; border: 1px solid;}
.pct-0   {background: #fcebeb; border-color: #b3261e;}
.pct-50  {background: #e1f5ee; border-color: #0f6e56;}
.pct-100 {background: #eaf3de; border-color: #27500a;}
.np      {background: #e8eef8; border-color: #1d5fa8;}
.ask     {background: #fde3d3; border-color: #8a4315;}
.result-card .head {font-weight: 700; font-size: 1.1rem; margin-bottom: 4px;}
.result-card .cat {font-weight: 600; margin-bottom: 6px;}
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
    st.title("🧾 Meals & Entertainment Guidance")
    entered = st.text_input("Team password", type="password")
    if entered:
        if hmac.compare_digest(entered, pw):
            st.session_state.authed = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


gate()

# ---------- state ----------
ss = st.session_state
ss.setdefault("messages", [])      # API conversation for the current expense
ss.setdefault("tool_id", None)     # pending clarifying-question tool call
ss.setdefault("result", None)
ss.setdefault("harvested", [])
ss.setdefault("form_id", 0)        # bumped to clear inputs after a final answer


@st.cache_resource
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])


def call(new_message: dict) -> None:
    msgs = ss.messages + [new_message]
    try:
        with st.spinner("Reviewing the firm guides and checking current IRS guidance…"):
            result, tool_id, harvested = engine.run(client(), MODEL, msgs, SEARCH_TOOL)
    except (engine.GuidanceError, anthropic.APIError) as e:
        st.error(f"Something went wrong: {e}")
        return
    ss.messages, ss.tool_id, ss.result = msgs, tool_id, result
    ss.harvested = harvested
    if result.get("status") == "final":
        ss.messages, ss.tool_id = [], None   # next submission starts fresh
        ss.form_id += 1                       # clears description + upload


def result_text(r: dict, entity: str) -> str:
    lines = [f"Entity type: {entity}", f"Category: {r.get('category')}"]
    if r.get("deductible_percent") is not None:
        lines.append(f"Deductible: {r['deductible_percent']}%")
    if r.get("functional_class"):
        lines.append(f"Functional class: {r['functional_class']}")
    lines += ["", f"Reasoning: {r.get('reasoning', '')}"]
    if r.get("receipt_summary"):
        lines.append(f"Receipt: {r['receipt_summary']}")
    wc = r.get("web_check", {})
    lines += ["", f"Web check: {wc.get('result', '')} - {wc.get('summary', '')}"]
    for d in r.get("discrepancies", []):
        lines += [f"DISCREPANCY ({d['topic']}): guide says {d['guide_says']}; web says {d['web_says']}; "
                  f"treatment per web: {d['treatment_per_web']}"]
    if r.get("caveats"):
        lines.append(f"Caveats: {r['caveats']}")
    for s in r.get("sources", []):
        lines.append(f"Source: {s['title']} - {s['url']}")
    return "\n".join(lines)


# ---------- header ----------
st.title("🧾 Meals & Entertainment Guidance")
st.info(
    "Describe the expense and optionally attach the receipt or invoice. Claude checks it against the firm's "
    "M&E guides and searches current IRS guidance, and flags any conflict between the two. Uploads are sent "
    "to Anthropic for processing; this app saves nothing. Bookkeeping guidance, not tax or legal advice.",
    icon="ℹ️",
)

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


st.markdown("**📘 Current rules & flowcharts**")
for title, path in engine.GUIDES:
    with st.expander(f"View: {title}"):
        st.download_button(f"Download PDF", path.read_bytes(), file_name=path.name,
                           mime="application/pdf", key=f"dl_{path.stem}")
        for png in guide_pages(str(path)):
            st.image(png, width="stretch")
st.divider()

fid = ss.form_id
entity = st.radio("Client type", ["Business (taxable)", "Nonprofit"], horizontal=True, key="entity")
desc = st.text_area(
    "Describe the expense",
    placeholder="e.g. Took a prospective client to dinner to discuss a new engagement",
    key=f"desc_{fid}",
)
with st.container(border=True):
    st.markdown("**📎 Receipt or invoice (optional)**")
    upload = st.file_uploader(
        "Drag a file into the box below, or click Browse files. PDF, PNG, JPG, WEBP, or GIF.",
        type=["pdf", "png", "jpg", "jpeg", "webp", "gif"],
        key=f"file_{fid}",
    )

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
        ss.messages, ss.tool_id, ss.result = [], None, None
        ss.last_entity = entity
        call(engine.first_user_message(desc, entity, att))
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
            f'<ol style="margin:8px 0 0 0;font-weight:600">{q_items}</ol></div>',
            unsafe_allow_html=True,
        )
        with st.form("reply", clear_on_submit=True):
            label = "Your answer" if len(questions) <= 1 else "Your answers (answer each question)"
            reply = st.text_area(label, height=80 if len(questions) <= 1 else 120)
            if st.form_submit_button("Reply", type="primary") and reply.strip():
                call(engine.reply_message(ss.tool_id, reply))
                st.rerun()
    else:
        pct = r.get("deductible_percent")
        if pct is not None:
            css, head = f"pct-{pct}", f"{pct}% deductible"
            if r.get("functional_class"):
                head += f" · {r['functional_class']}"
        else:
            css, head = "np", r.get("functional_class") or "Functional class"
        st.markdown(
            f'<div class="result-card {css}"><div class="head">{html.escape(head)}</div>'
            f'<div class="cat">{html.escape(r.get("category") or "")}</div>'
            f'<div>{html.escape(r.get("reasoning", ""))}</div></div>',
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

        if r.get("receipt_summary"):
            st.caption(f"**From the attachment:** {r['receipt_summary']}")
        if r.get("caveats"):
            st.caption(f"**Note:** {r['caveats']}")

        sources = r.get("sources") or ss.harvested[:6]
        if sources:
            with st.expander("Sources checked"):
                for s in sources:
                    st.markdown(f"- [{s['title']}]({s['url']})")

        st.download_button("Save this answer (.txt)", result_text(r, entity_used),
                           file_name="ME_guidance.txt", mime="text/plain")
