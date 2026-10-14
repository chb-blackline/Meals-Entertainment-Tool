"""Talks to the Anthropic API. Kept separate from the UI so it can be tested."""

from __future__ import annotations

import base64
import io
from datetime import date
from pathlib import Path

from PIL import Image

from prompts import SUBMIT_TOOL, SYSTEM_PROMPT

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
MAX_IMAGE_SIDE = 2000
MAX_LOOPS = 6  # guards against an endless pause_turn / retry cycle


class GuidanceError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def guide_blocks() -> list[dict]:
    """Both guides as PDF document blocks. The cache breakpoint on the last one lets
    the system prompt, tools, and guides be reused across requests at lower cost."""
    blocks = []
    for i, (title, path) in enumerate(GUIDES):
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(path.read_bytes())},
            "title": f"FIRM GUIDE: {title}",
        }
        if i == len(GUIDES) - 1:
            block["cache_control"] = {"type": "ephemeral"}
        blocks.append(block)
    return blocks


def attachment_block(data: bytes, mime: str, name: str) -> dict:
    """Receipt/invoice as an image or PDF block. PDFs are sent whole (all pages)."""
    if mime == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)},
            "title": f"RECEIPT/INVOICE: {name}",
        }
    if mime in IMAGE_TYPES:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > MAX_IMAGE_SIDE or len(data) > 4_500_000:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            data, mime = buf.getvalue(), "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}
    raise GuidanceError(f"Unsupported file type ({mime}). Use a PDF, PNG, JPG, WEBP, or GIF.")


def first_user_message(description: str, entity_type: str, attachment: dict | None) -> dict:
    content = guide_blocks()
    if attachment:
        content.append(attachment)
    text = description.strip() or "See the attached receipt/invoice."
    content.append({"type": "text", "text": f"Entity type: {entity_type}\n\nExpense description: {text}"})
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


def run(client, model: str, messages: list[dict], search_tool: dict) -> tuple[dict, str, list[dict]]:
    """Runs one turn. `messages` is mutated in place (assistant turns appended).
    Returns (guidance, tool_use_id, harvested_sources)."""
    system = [{"type": "text", "text": SYSTEM_PROMPT.format(today=date.today().strftime("%B %d, %Y"))}]
    tools = [search_tool, SUBMIT_TOOL]
    harvested: list[dict] = []
    tool_choice = {"type": "auto"}

    for _ in range(MAX_LOOPS):
        resp = client.messages.create(
            model=model, max_tokens=4096, system=system, tools=tools,
            tool_choice=tool_choice, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        harvested += _harvest_urls(resp.content)

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_guidance":
                return dict(block.input), block.id, harvested

        if resp.stop_reason == "pause_turn":
            continue  # long server-side search; resend to let it finish
        if resp.stop_reason == "refusal":
            raise GuidanceError("Claude declined this request. Try rephrasing the description.")
        # Ended without calling the tool: ask it to, and force the tool on the retry.
        messages.append({"role": "user", "content": "Please submit your answer with the submit_guidance tool now."})
        tool_choice = {"type": "tool", "name": "submit_guidance"}

    raise GuidanceError("No answer came back after several attempts. Please try again.")
