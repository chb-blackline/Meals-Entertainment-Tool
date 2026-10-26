"""Talks to the Anthropic API. Kept separate from the UI so it can be tested."""

from __future__ import annotations

import base64
import io
import re
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from prompts import LINE_ITEMS_TOOL, SUBMIT_TOOL, SYSTEM_PROMPT

APP_DIR = Path(__file__).parent


def _find(name: str) -> Path:
    """Guides may live in /reference or at the top level of the repo."""
    for folder in (APP_DIR / "reference", APP_DIR):
        if (folder / name).exists():
            return folder / name
    raise FileNotFoundError(
        f"Guide '{name}' not found. Put it in the repo's top level or a 'reference' folder, named exactly that."
    )


GUIDES = [
    ("General (taxable business) methodology", _find("ME_Methodology_General.pdf")),
    ("Nonprofit methodology", _find("ME_Methodology_Nonprofit.pdf")),
]

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_SIDE = 1568   # Claude downsizes anything larger anyway
MAX_RECEIPT_PAGES = 3
MAX_LOOPS = 8  # guards against an endless pause_turn / retry cycle


LINE_REQUEST = re.compile(r"\beach (charge|line|item)|line[- ]by[- ]line|line items?|itemi[sz]|every (charge|line)|"
                          r"each of the (charges|lines|items)", re.I)
PCTS = re.compile(r"\b(0|50|100)\s?%")


def _user_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    return " ".join(parts)


def _needs_line_items(result: dict, messages: list[dict]) -> str | None:
    """Why a final answer needs line items it doesn't have: "asked" (the user asked about each charge) or
    "mixed" (an upload whose answer names more than one percentage). None if it's fine as is."""
    if result.get("status") != "final" or result.get("line_items"):
        return None
    if LINE_REQUEST.search(_user_text(messages)):
        return "asked"
    has_attachment = any(isinstance(b, dict) and b.get("type") in ("document", "image")
                         for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
                         for b in m["content"])
    answer = " ".join(str(result.get(k) or "") for k in ("reasoning", "caveats"))
    if has_attachment and len(set(PCTS.findall(answer))) > 1:
        return "mixed"
    return None



def _answered_questions(messages: list[dict]) -> bool:
    """True once the bookkeeper has replied to at least one round of clarifying questions."""
    return any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("content") == "Question shown to the bookkeeper."
               for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
               for b in m["content"])


class GuidanceError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def _pdf_text(path: Path) -> str:
    pdf = pdfium.PdfDocument(str(path))
    text = "\n".join(page.get_textpage().get_text_range() for page in pdf)
    pdf.close()
    return text


# Guides are sent as plain text: the same words at a fraction of the tokens of PDF page images.
GUIDE_TEXT = "\n\n".join(f"===== FIRM GUIDE: {title} =====\n{_pdf_text(path)}" for title, path in GUIDES)


def attachment_block(data: bytes, mime: str, name: str) -> dict:
    """Receipt/invoice as an image or PDF block (PDFs capped at the first few pages)."""
    if mime == "application/pdf":
        src = pdfium.PdfDocument(data)
        if len(src) > MAX_RECEIPT_PAGES:
            trimmed = pdfium.PdfDocument.new()
            trimmed.import_pages(src, list(range(MAX_RECEIPT_PAGES)))
            buf = io.BytesIO()
            trimmed.save(buf)
            data = buf.getvalue()
            trimmed.close()
        src.close()
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)},
            "title": f"RECEIPT/INVOICE: {name}",
        }
    if mime in IMAGE_TYPES:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > MAX_IMAGE_SIDE or len(data) > 1_500_000:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            data, mime = buf.getvalue(), "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}
    raise GuidanceError(f"Unsupported file type ({mime}). Use a PDF, PNG, JPG, WEBP, or GIF.")


def first_user_message(description: str, entity_type: str, attachment: dict | None) -> dict:
    content = []
    if attachment:
        content.append(attachment)
    text = description.strip() or "See the attached receipt/invoice."
    att_note = "A receipt/invoice is attached." if attachment else "NO attachment was provided; set receipt_summary to null."
    content.append({"type": "text", "text": f"Entity type: {entity_type}\n{att_note}\n\nExpense description: {text}"})
    return {"role": "user", "content": content}


def reply_message(tool_use_id: str, reply_text: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "Question shown to the bookkeeper."},
            {"type": "text", "text": reply_text.strip()},
        ],
    }


def _harvest_urls(content) -> list[dict]:
    """Backup source list: every page the web search returned."""
    seen, out = set(), []
    for block in content:
        if getattr(block, "type", None) == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                url = getattr(r, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    out.append({"title": getattr(r, "title", url) or url, "url": url})
    return out


def run(client, model: str, messages: list[dict], search_tool: dict) -> tuple[dict, str, list[dict], dict]:
    """Runs one turn. `messages` is mutated in place (assistant turns appended).
    Returns (guidance, tool_use_id, harvested_sources, usage)."""
    system = [
        {"type": "text", "text": SYSTEM_PROMPT + "\n\n" + GUIDE_TEXT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Today's date is {date.today():%B %d, %Y}."},  # after the cache point
    ]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0, "calls": 0}
    tools = [search_tool, SUBMIT_TOOL]
    harvested: list[dict] = []
    tool_choice = {"type": "auto"}
    line_retry_done, why, first_result = False, None, None
    ask_retry_done = False

    for _ in range(MAX_LOOPS):
        resp = client.messages.create(
            model=model, max_tokens=8000, system=system, tools=tools,
            tool_choice=tool_choice, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        harvested += _harvest_urls(resp.content)
        u = resp.usage
        usage["calls"] += 1
        usage["input"] += getattr(u, "input_tokens", 0) or 0
        usage["output"] += getattr(u, "output_tokens", 0) or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

        submitted = next((b for b in resp.content
                          if getattr(b, "type", None) == "tool_use" and b.name == "submit_guidance"), None)
        if submitted:
            result = dict(submitted.input)
            if (not ask_retry_done and result.get("status") == "final" and result.get("assumptions")
                    and not _answered_questions(messages)):
                # Material facts were assumed without asking: send it back to ask instead.
                ask_retry_done = True
                listed = "; ".join(result["assumptions"])
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": submitted.id,
                     "content": "Assumed facts instead of asking.", "is_error": True},
                    {"type": "text", "text": (
                        f"You assumed: {listed}. Don't assume facts that change the treatment. Resubmit with "
                        "status needs_info and ask the bookkeeper about them (up to three questions, grouped). "
                        "No new web searches.")},
                ]})
                tool_choice = {"type": "tool", "name": "submit_guidance"}
                continue
            why = None if line_retry_done else _needs_line_items(result, messages)
            if not why:
                return result, submitted.id, harvested, usage
            # Redo with a tool that can only be completed with a list of charges.
            line_retry_done, first_result = True, result
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": submitted.id,
                 "content": "Line items required.", "is_error": True},
                {"type": "text", "text": (
                    "List EVERY charge on the statement or invoice with submit_line_items, in statement order: "
                    "date, description, amount (incl. service charge and tax; negative for credits), treatment, "
                    "category, and a note under 15 words. Use the facts already given. No new web searches.")},
            ]})
            tools = [search_tool, SUBMIT_TOOL, LINE_ITEMS_TOOL]
            tool_choice = {"type": "tool", "name": "submit_line_items"}
            continue

        lined = next((b for b in resp.content
                      if getattr(b, "type", None) == "tool_use" and b.name == "submit_line_items"), None)
        if lined:
            lines = list(lined.input.get("line_items") or [])
            treatments = {ln.get("treatment") for ln in lines if ln.get("treatment") != "Not an expense"}
            if why == "mixed" and len(treatments) <= 1:
                return first_result, lined.id, harvested, usage   # one treatment after all: keep the plain answer
            merged = {**first_result, "line_items": lines, "category": None, "deductible_percent": None,
                      "functional_class": None, "reasoning": lined.input.get("reasoning") or first_result.get("reasoning"),
                      "caveats": lined.input.get("caveats", first_result.get("caveats"))}
            return merged, lined.id, harvested, usage

        if resp.stop_reason == "pause_turn":
            continue  # long server-side search; resend to let it finish
        if resp.stop_reason == "refusal":
            raise GuidanceError("Claude declined this request. Try rephrasing the description.")
        # Ended without calling the tool: ask it to, and force the tool on the retry.
        messages.append({"role": "user", "content": "Please submit your answer with the submit_guidance tool now."})
        tool_choice = {"type": "tool", "name": "submit_guidance"}

    raise GuidanceError("No answer came back after several attempts. Please try again.")
