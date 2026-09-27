"""Period reports (month / quarter / year) and single-task reports as PDF files."""
import io
import os
import re
from collections import Counter
from datetime import date, datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import texts as T

OPEN = ("new", "progress", "blocked")
PLAIN_STATUS = {"new": "New", "progress": "In progress", "blocked": "Waiting / Blocked",
                "approval": "Needs approval", "done": "Done", "rejected": "Rejected"}
PLAIN_PRIORITY = {"low": "Low", "med": "Medium", "high": "High", "urg": "Urgent"}

INK = colors.HexColor("#0B0F0D")
GREEN = colors.HexColor("#1FB866")
GREEN_SOFT = colors.HexColor("#E7F7EE")
MUTED = colors.HexColor("#5E6F66")
LINE = colors.HexColor("#D5DED9")
RED = colors.HexColor("#C23B33")

# ---------- fonts (DejaVu covers Latin + Cyrillic; falls back to Helvetica) ----------
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", os.path.join(os.path.dirname(__file__), "fonts")]
FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"
for _d in _FONT_DIRS:
    if os.path.exists(os.path.join(_d, "DejaVuSans.ttf")):
        pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(_d, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(_d, "DejaVuSans-Bold.ttf")))
        FONT, FONT_BOLD = "DejaVu", "DejaVu-Bold"
        break

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def clean(s):
    """PDF fonts have no emoji: drop them and tidy spaces."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", str(s or ""))).strip()


def esc(s):
    return clean(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- periods ----------
KINDS = ("month", "quarter", "year")


def period_bounds(kind, value):
    """-> (start 'YYYY-MM-DD', end 'YYYY-MM-DD' exclusive, human label). Raises ValueError on bad input."""
    if kind == "month":
        y, m = map(int, value.split("-"))
        start = date(y, m, 1)
        end = date(y + (m == 12), m % 12 + 1, 1)
        label = f"{T.MONTH_FULL[m - 1]} {y}"
    elif kind == "quarter":
        y, q = value.split("-Q")
        y, q = int(y), int(q)
        if not 1 <= q <= 4:
            raise ValueError(value)
        start = date(y, 3 * q - 2, 1)
        end = date(y + (q == 4), (3 * q) % 12 + 1, 1)
        label = f"Q{q} {y} ({T.MON[3 * q - 3]}–{T.MON[3 * q - 1]})"
    elif kind == "year":
        y = int(value)
        start, end, label = date(y, 1, 1), date(y + 1, 1, 1), str(y)
    else:
        raise ValueError(kind)
    return start.isoformat(), end.isoformat(), label


def period_options(kind, today, count=None):
    """Recent periods, newest first: [(value, label), ...]."""
    out = []
    if kind == "month":
        y, m = today.year, today.month
        for _ in range(count or 24):
            out.append((f"{y}-{m:02d}", f"{T.MONTH_FULL[m - 1]} {y}"))
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    elif kind == "quarter":
        y, q = today.year, (today.month - 1) // 3 + 1
        for _ in range(count or 8):
            out.append((f"{y}-Q{q}", period_bounds("quarter", f"{y}-Q{q}")[2]))
            y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    else:
        out = [(str(y), str(y)) for y in range(today.year, today.year - (count or 5), -1)]
    return out


def current_value(kind, today):
    return period_options(kind, today, 1)[0][0]


# ---------- numbers ----------
def summarize(db, kind, value):
    start, end, label = period_bounds(kind, value)
    tasks = {t["id"]: t for t in db.tasks()}
    members = {m["id"]: m for m in db.members(active_only=False)}
    rows = db.history_between(start, end)
    today = db.today()

    touched = {r["task_id"] for r in rows if r["task_id"] in tasks}
    touched |= {tid for tid, t in tasks.items() if start <= t["created_at"][:10] < end}
    created = [tid for tid, t in tasks.items() if start <= t["created_at"][:10] < end]
    completed = sorted({r["task_id"] for r in rows if "→ ✅ Done" in r["text"]})
    rejected = sorted({r["task_id"] for r in rows if "→ ⛔ Rejected" in r["text"]})
    in_period = [tasks[i] for i in sorted(touched)]
    still_open = [t for t in in_period if t["status"] in OPEN + ("approval",)]
    overdue = [t for t in in_period if T.is_overdue(t, today)]

    people = {}
    for t in in_period:
        who = t["assignee"]
        p = people.setdefault(who, {"assigned": 0, "done": 0, "open": 0, "overdue": 0})
        p["assigned"] += 1
        p["open"] += t["status"] in OPEN + ("approval",)
        p["overdue"] += T.is_overdue(t, today)
    for tid in completed:
        t = tasks.get(tid)
        if t:
            who = t["done_by"] or t["assignee"]
            people.setdefault(who, {"assigned": 0, "done": 0, "open": 0, "overdue": 0})["done"] += 1

    return {
        "kind": kind, "value": value, "label": label, "start": start, "end": end,
        "tasks": in_period, "history": rows, "members": members,
        "created": len(created), "completed": len(completed), "rejected": len(rejected),
        "open": len(still_open), "overdue": len(overdue), "active": len(in_period),
        "people": people, "categories": Counter(t["category"] for t in in_period),
        "status": Counter(t["status"] for t in in_period),
    }


# ---------- PDF ----------
def _styles():
    base = dict(fontName=FONT, fontSize=9, leading=12, textColor=INK, alignment=TA_LEFT)
    return {
        "h1": ParagraphStyle("h1", **{**base, "fontName": FONT_BOLD, "fontSize": 20, "leading": 24}),
        "h2": ParagraphStyle("h2", **{**base, "fontName": FONT_BOLD, "fontSize": 12.5, "leading": 16,
                                      "spaceBefore": 10, "spaceAfter": 6}),
        "sub": ParagraphStyle("sub", **{**base, "fontSize": 10, "textColor": MUTED}),
        "cell": ParagraphStyle("cell", **base),
        "cellb": ParagraphStyle("cellb", **{**base, "fontName": FONT_BOLD}),
        "small": ParagraphStyle("small", **{**base, "fontSize": 8, "leading": 10, "textColor": MUTED}),
        "kpi": ParagraphStyle("kpi", **{**base, "fontName": FONT_BOLD, "fontSize": 18, "leading": 22}),
    }


def _page(canvas, doc):
    w, h = doc.pagesize
    canvas.saveState()
    canvas.setFillColor(INK)
    canvas.rect(0, h - 16 * mm, w, 16 * mm, stroke=0, fill=1)
    canvas.setFillColor(GREEN)
    canvas.circle(14 * mm, h - 8 * mm, 1.6 * mm, stroke=0, fill=1)
    canvas.circle(18.5 * mm, h - 8 * mm, 1.6 * mm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont(FONT_BOLD, 11)
    canvas.drawString(23 * mm, h - 9.6 * mm, "ALGO GROUP")
    canvas.setFillColor(GREEN)
    canvas.drawString(23 * mm + canvas.stringWidth("ALGO GROUP ", FONT_BOLD, 11), h - 9.6 * mm, "CASER")
    canvas.setFillColor(colors.HexColor("#9FB3A8"))
    canvas.setFont(FONT, 8.5)
    canvas.drawRightString(w - 12 * mm, h - 9.6 * mm, doc.title_right)
    canvas.setFillColor(MUTED)
    canvas.setFont(FONT, 8)
    canvas.drawString(12 * mm, 8 * mm, "ALGO GROUP · CASER · Task report · Confidential")
    canvas.drawRightString(w - 12 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _table(data, widths, header=True, zebra=True):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    if zebra:
        for i in range(2 if header else 1, len(data), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F4F8F6")))
    t.setStyle(TableStyle(style))
    return t


def _hdr(cells, st):
    return [Paragraph(f'<font color="white">{c}</font>', st["cellb"]) for c in cells]


def _task_rows(tasks, members, today, st):
    rows = [_hdr(["ID", "Task", "Assignee", "Company", "Category", "Priority", "Deadline", "Status"], st)]
    for t in tasks:
        status = PLAIN_STATUS[t["status"]] + (" · OVERDUE" if T.is_overdue(t, today) else "")
        color = "#C23B33" if T.is_overdue(t, today) else ("#1B8A4F" if t["status"] == "done" else "#0B0F0D")
        rows.append([
            Paragraph(T.code(t["id"]), st["cellb"]),
            Paragraph(esc(t["title"]), st["cell"]),
            Paragraph(esc(T.name_of(members, t["assignee"])), st["cell"]),
            Paragraph(esc(t["company"]), st["cell"]),
            Paragraph(esc(t["category"]), st["cell"]),
            Paragraph(PLAIN_PRIORITY.get(t["priority"], ""), st["cell"]),
            Paragraph(esc(T.fmt_date(t["deadline"])), st["cell"]),
            Paragraph(f'<font color="{color}">{esc(status)}</font>', st["cellb"]),
        ])
    return rows


def _who(members, who):
    return "Website" if who == "WEB" else T.name_of(members, who)


def _history_block(task, entries, members, st, width):
    rows = [[Paragraph(f"<b>{T.code(task['id'])}</b> · {esc(task['title'])}", st["cellb"]), "", "", ""]]
    for r in entries:
        rows.append([Paragraph(esc(T.fmt_stamp(r["at"])), st["small"]), Paragraph(esc(_who(members, r["who"])), st["cell"]),
                     Paragraph(esc(r["text"]), st["cell"]),
                     Paragraph("Website" if r["source"] == "web" else "Telegram", st["small"])])
    t = Table(rows, colWidths=[width * 0.16, width * 0.14, width * 0.58, width * 0.12])
    t.setStyle(TableStyle([
        ("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), GREEN_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return KeepTogether([t, Spacer(1, 6)]) if len(rows) < 25 else t


def _doc(buf, title_right):
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=22 * mm, bottomMargin=14 * mm, title="ALGO GROUP CASER report",
                            author="ALGO GROUP CASER")
    doc.title_right = title_right
    return doc


def period_pdf(db, kind, value, include_history=True):
    s = summarize(db, kind, value)
    st = _styles()
    buf = io.BytesIO()
    generated = datetime.now(db.tz).strftime("%d %b %Y %H:%M")
    doc = _doc(buf, f"{kind.capitalize()} report · {s['label']}")
    width = doc.width
    members, today = s["members"], db.today()
    story = [Paragraph(f"Task report · {esc(s['label'])}", st["h1"]),
             Paragraph(f"Period {T.fmt_date(s['start'])} – {T.fmt_date(date.fromordinal(date.fromisoformat(s['end']).toordinal() - 1).isoformat())}"
                       f" · Generated {generated} (Tashkent)", st["sub"]), Spacer(1, 10)]

    kpis = [("Active tasks", s["active"]), ("Created", s["created"]), ("Completed", s["completed"]),
            ("Rejected", s["rejected"]), ("Still open", s["open"]), ("Overdue now", s["overdue"])]
    cells = [[Paragraph(f'<font color="{"#C23B33" if k == "Overdue now" and v else "#0B0F0D"}">{v}</font>', st["kpi"])
              for k, v in kpis], [Paragraph(k, st["small"]) for k, _ in kpis]]
    kt = Table(cells, colWidths=[width / 6] * 6)
    kt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                            ("LINEABOVE", (0, 0), (-1, 0), 2.2, GREEN), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
    story += [kt, Spacer(1, 8)]

    # people + categories side by side
    prow = [_hdr(["Person", "Tasks", "Done", "Open", "Overdue"], st)]
    for who, p in sorted(s["people"].items(), key=lambda kv: (-kv[1]["assigned"], T.name_of(members, kv[0]))):
        prow.append([Paragraph(esc(T.name_of(members, who)), st["cell"])] +
                    [Paragraph(str(p[k]), st["cell"]) for k in ("assigned", "done", "open", "overdue")])
    if len(prow) == 1:
        prow.append([Paragraph("No tasks in this period.", st["small"]), "", "", "", ""])
    crow = [_hdr(["Category", "Tasks"], st)] + [[Paragraph(esc(c), st["cell"]), Paragraph(str(n), st["cell"])]
                                                for c, n in s["categories"].most_common()]
    srow = [_hdr(["Status now", "Tasks"], st)] + [[Paragraph(PLAIN_STATUS[k], st["cell"]), Paragraph(str(s["status"][k]), st["cell"])]
                                                  for k in PLAIN_STATUS if s["status"][k]]
    left_w, mid_w = width * 0.46, width * 0.25
    side = Table([[_table(prow, [left_w * .4, left_w * .15, left_w * .17, left_w * .13, left_w * .15]),
                   _table(crow, [mid_w * .7, mid_w * .3]) if len(crow) > 1 else "",
                   _table(srow, [mid_w * .7, mid_w * .3]) if len(srow) > 1 else ""]],
                 colWidths=[left_w + 8, mid_w + 8, mid_w])
    side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Paragraph("Team and categories", st["h2"]), side]

    story.append(Paragraph(f"Tasks in this period ({len(s['tasks'])})", st["h2"]))
    if s["tasks"]:
        w = width
        story.append(_table(_task_rows(s["tasks"], members, today, st),
                            [w * .07, w * .29, w * .11, w * .15, w * .10, w * .07, w * .10, w * .11]))
    else:
        story.append(Paragraph("No tasks were created or changed in this period.", st["sub"]))

    if include_history and s["history"]:
        story.append(Paragraph("History (every change in this period)", st["h2"]))
        by_task = {}
        for r in s["history"]:
            by_task.setdefault(r["task_id"], []).append(r)
        tasks = {t["id"]: t for t in db.tasks()}
        for tid in sorted(by_task):
            if tid in tasks:
                story.append(_history_block(tasks[tid], by_task[tid], members, st, width))
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


def task_pdf(db, task_id):
    t = db.get_task(task_id)
    members = {m["id"]: m for m in db.members(active_only=False)}
    st = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"Task {T.code(task_id)}")
    width, today = doc.width, db.today()
    story = [Paragraph(f"{T.code(task_id)} · {esc(t['title'])}", st["h1"]),
             Paragraph(f"Generated {datetime.now(db.tz).strftime('%d %b %Y %H:%M')} (Tashkent)", st["sub"]), Spacer(1, 10),
             _table(_task_rows([t], members, today, st),
                    [width * .07, width * .29, width * .11, width * .15, width * .10, width * .07, width * .10, width * .11])]
    details = [("Description", t["description"]), ("Notes", t["notes"]), ("Reason", t["reason"]),
               ("Latest note", f"{t['last_note']} ({T.name_of(members, t['last_note_by'])}, {T.fmt_stamp(t['last_note_at'])})" if t["last_note"] else ""),
               ("Created", f"{T.fmt_stamp(t['created_at'])} by {T.name_of(members, t['creator'])}")]
    rows = [[Paragraph(k, st["cellb"]), Paragraph(esc(v), st["cell"])] for k, v in details if v]
    story += [Spacer(1, 8), _table(rows, [width * .15, width * .85], header=False, zebra=False)]
    story += [Paragraph("History", st["h2"]), _history_block(t, db.history(task_id, include_web=True), members, st, width)]
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()
