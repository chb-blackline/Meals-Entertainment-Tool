"""One-page PDF of a guidance result, sized to attach to a QBO transaction."""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import KeepInFrame, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Same colors as the guides and the app.
TONE = {
    0: (colors.HexColor("#fcebeb"), colors.HexColor("#b3261e")),
    50: (colors.HexColor("#e1f5ee"), colors.HexColor("#0f6e56")),
    100: (colors.HexColor("#eaf3de"), colors.HexColor("#27500a")),
    None: (colors.HexColor("#e8eef8"), colors.HexColor("#1d5fa8")),
}
INK = colors.HexColor("#1f2937")
DIM = colors.HexColor("#6b7280")

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=12.5, textColor=INK, alignment=TA_LEFT)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=8, leading=10, textColor=DIM)
H1 = ParagraphStyle("h1", parent=BASE, fontName="Helvetica-Bold", fontSize=15, leading=18)
H2 = ParagraphStyle("h2", parent=BASE, fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=8, spaceAfter=3)
BIG = ParagraphStyle("big", parent=BASE, fontName="Helvetica-Bold", fontSize=14, leading=17)
BOLD = ParagraphStyle("bold", parent=BASE, fontName="Helvetica-Bold")


def _t(s) -> str:
    """Escape for Paragraph markup and drop characters the built-in fonts can't draw (e.g. emoji)."""
    s = escape(str(s or ""))
    return s.encode("cp1252", errors="ignore").decode("cp1252")


def _box(flowables, bg, border, pad=8):
    t = Table([[flowables]], colWidths=[7.0 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 1, border),
        ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]))
    return t


def build_pdf(r: dict, entity: str, description: str, filename: str | None, qa: list[dict]) -> bytes:
    story = []
    story.append(Paragraph("Meals &amp; Entertainment Guidance", H1))
    story.append(Paragraph(
        f"BlackLine Solutions, LLC &nbsp;|&nbsp; Prepared {datetime.now(ZoneInfo('America/New_York')).strftime('%B %d, %Y %I:%M %p ET').replace(' 0', ' ')}"
        f" &nbsp;|&nbsp; Client type: {_t(entity)}", SMALL))
    story.append(Spacer(1, 8))

    # Expense
    story.append(Paragraph("Expense", H2))
    story.append(Paragraph(_t(description) or "<i>No description entered; classified from the attachment.</i>", BASE))
    if filename:
        story.append(Paragraph(f"Attachment: {_t(filename)}", SMALL))
    if r.get("receipt_summary"):
        story.append(Paragraph(f"<b>From the attachment:</b> {_t(r['receipt_summary'])}", BASE))
    for item in qa:
        for q in item["questions"]:
            story.append(Paragraph(f"<b>Q:</b> {_t(q)}", BASE))
        story.append(Paragraph(f"<b>A:</b> {_t(item['answer'])}", BASE))

    # Result
    pct = r.get("deductible_percent")
    bg, border = TONE.get(pct, TONE[None])
    if pct is not None:
        head = f"{pct}% deductible"
        if r.get("functional_class"):
            head += f"  |  {_t(r['functional_class'])}"
    else:
        head = _t(r.get("functional_class") or "Functional class")
    story.append(Paragraph("Result", H2))
    story.append(_box([
        Paragraph(head, ParagraphStyle("hd", parent=BIG, textColor=border)),
        Paragraph(_t(r.get("category")), BOLD),
        Spacer(1, 3),
        Paragraph(_t(r.get("reasoning")), BASE),
    ], bg, border))

    # Web check
    wc = r.get("web_check") or {}
    disc = r.get("discrepancies") or []
    story.append(Paragraph("Current-law check", H2))
    if disc or wc.get("result") == "discrepancy":
        parts = [Paragraph("DISCREPANCY: current sources differ from the firm guide. "
                           "The result above follows the guide; review before relying on it.",
                           ParagraphStyle("dh", parent=BOLD, textColor=TONE[0][1]))]
        for d in disc:
            parts += [Spacer(1, 3), Paragraph(f"<b>{_t(d.get('topic'))}</b>", BASE),
                      Paragraph(f"Guide says: {_t(d.get('guide_says'))}", BASE),
                      Paragraph(f"Current sources say: {_t(d.get('web_says'))}", BASE),
                      Paragraph(f"Treatment per current sources: {_t(d.get('treatment_per_web'))}", BASE)]
        story.append(_box(parts, TONE[0][0], TONE[0][1]))
    else:
        label = {"agrees": "Agrees with the firm guide.", "inconclusive": "Inconclusive."}.get(
            wc.get("result"), "")
        story.append(Paragraph(f"<b>{label}</b> {_t(wc.get('summary'))}", BASE))

    if r.get("caveats"):
        story.append(Paragraph("Notes", H2))
        story.append(Paragraph(_t(r["caveats"]), BASE))

    sources = r.get("sources") or []
    if sources:
        story.append(Paragraph("Sources checked", H2))
        for s in sources[:6]:
            url = _t(s.get("url"))
            story.append(Paragraph(f"{_t(s.get('title'))}: <link href=\"{url}\" color=\"#1d5fa8\">{url}</link>", SMALL))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Prepared with BlackLine's Meals &amp; Entertainment guides and a live check of current IRS guidance. "
        "Bookkeeping guidance, not tax or legal advice.", SMALL))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title="Meals & Entertainment Guidance", author="BlackLine Solutions, LLC")
    # Shrink to fit so the report always stays on one page.
    doc.build([KeepInFrame(doc.width, doc.height, story, mode="shrink")])
    return buf.getvalue()
