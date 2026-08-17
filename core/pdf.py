"""Month-end attendance statement rendered as a PDF.

The monthly mail already carries the numbers in its body; the attachment is the
copy an employee can file, print or forward. Both are built from the same
``summary``/``records`` pair, so they can never disagree.

Amounts are written as ``Rs`` rather than the rupee sign: the built-in Type 1
fonts have no glyph for it, and an embedded font would put a megabyte on every
mail for one character.
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from django.utils.html import escape

from .models import Attendance
from .templatetags.money import rupees, rupees_short

PRESENT = colors.HexColor("#16a34a")
LATE = colors.HexColor("#ea580c")
INK = colors.HexColor("#111827")
BODY = colors.HexColor("#374151")
MUTED = colors.HexColor("#6b7280")
FAINT = colors.HexColor("#9ca3af")
LINE = colors.HexColor("#e5e7eb")
HAIRLINE = colors.HexColor("#f1f2f4")
WASH = colors.HexColor("#fafbfc")

STATUS_INK = {
    Attendance.PRESENT: "#16a34a",
    Attendance.HALF_DAY: "#d97706",
    Attendance.ABSENT: "#dc2626",
    Attendance.LEAVE: "#2563eb",
}

PAGE_MARGIN = 16 * mm

_body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=14, textColor=BODY)
_lead = ParagraphStyle("lead", parent=_body, fontSize=10, leading=15)
_cell = ParagraphStyle("cell", parent=_body, fontSize=9, leading=12, textColor=INK)
_cell_right = ParagraphStyle("cellRight", parent=_cell, alignment=2)
_head = ParagraphStyle(
    "head", fontName="Helvetica-Bold", fontSize=7.5, leading=10,
    textColor=MUTED, spaceAfter=0,
)
_section = ParagraphStyle(
    "section", fontName="Helvetica-Bold", fontSize=8, leading=11,
    textColor=FAINT, spaceBefore=6, spaceAfter=6,
)
_footnote = ParagraphStyle("footnote", parent=_body, fontSize=8.5, leading=12, textColor=MUTED)


def _accent(summary):
    """Green when the month holds up, orange when it does not. (Color, hex)."""
    if summary["attendance_pct"] >= 75:
        return PRESENT, "#16a34a"
    return LATE, "#ea580c"


def _banner(employee, organization, month_label, accent, width):
    """Coloured masthead: organisation, statement title, who it is for."""
    who = f"{escape(employee.name)} &middot; {escape(employee.employee_code)}"
    if employee.designation:
        who += f" &middot; {escape(employee.designation)}"
    lines = [
        [Paragraph(
            f'<font size="8" color="#ffffff">{escape(organization.name.upper())}</font>', _body
        )],
        [Paragraph(
            f'<font size="16" color="#ffffff"><b>Monthly Attendance — {month_label}</b></font>',
            _body,
        )],
        [Paragraph(f'<font size="9" color="#ffffff">{who}</font>', _body)],
    ]
    table = Table(lines, colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 14),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 14),
        ("TOPPADDING", (0, 1), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -2), 2),
    ]))
    return table


def _stat_row(cells, width):
    """A row of centred number-over-label tiles inside a hairline box."""
    row = [
        Paragraph(
            f'<para alignment="center"><font size="{size}" color="{value_color}">'
            f"<b>{value}</b></font><br/>"
            f'<font size="7" color="#6b7280">{label.upper()}</font></para>',
            _body,
        )
        for value, label, value_color, size in cells
    ]
    table = Table([row], colWidths=[width / len(row)] * len(row))
    style = [
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]
    for column in range(len(row) - 1):
        style.append(("LINEAFTER", (column, 0), (column, 0), 0.6, HAIRLINE))
    table.setStyle(TableStyle(style))
    return table


def _day_table(records, width):
    """Day-by-day ledger; the header repeats when the month spills over a page."""
    header = [Paragraph(text, _head) for text in ("DATE", "STATUS", "IN", "COLLECTED")]
    header[-1] = Paragraph('<para alignment="right">COLLECTED</para>', _head)
    rows = [header]

    for record in records:
        status = escape(record.get_status_display())
        if record.note:
            status += f' <font color="#9ca3af">&middot; {escape(record.note)}</font>'
        ink = STATUS_INK.get(record.status, "#6b7280")

        if record.check_in_time:
            check_in = record.check_in_time.strftime("%I:%M %p").lstrip("0")
            if record.is_late:
                check_in = (
                    f'<font color="#c2410c"><b>{check_in}</b> '
                    f"(+{record.late_by_display})</font>"
                )
        else:
            check_in = '<font color="#9ca3af">—</font>'

        collected = (
            f"Rs {rupees_short(record.total_collected)}"
            if record.has_business
            else '<font color="#9ca3af">—</font>'
        )

        rows.append([
            Paragraph(
                f'{record.date:%d %b} <font color="#9ca3af">{record.date:%a}</font>', _cell
            ),
            Paragraph(f'<font color="{ink}">{status}</font>', _cell),
            Paragraph(check_in, _cell),
            Paragraph(collected, _cell_right),
        ])

    table = Table(
        rows,
        colWidths=[width * 0.22, width * 0.42, width * 0.20, width * 0.16],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), WASH),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.5, HAIRLINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _page_furniture(organization, month_label):
    """Draw the footer rule, org line and page number on every page."""

    def draw(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(PAGE_MARGIN, 14 * mm, A4[0] - PAGE_MARGIN, 14 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(FAINT)
        footer = organization.name
        if organization.phone:
            footer += f" · {organization.phone}"
        canvas.drawString(PAGE_MARGIN, 9.5 * mm, f"{footer} · {month_label}")
        canvas.drawRightString(A4[0] - PAGE_MARGIN, 9.5 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    return draw


def build_monthly_pdf(employee, period_start, period_end, summary, records, as_on=None):
    """Return the employee's month statement as PDF bytes.

    ``summary`` and ``records`` are the same values the mail body renders, so the
    attachment is a restatement rather than a second calculation.
    """
    organization = employee.organization
    month_label = f"{period_start:%B %Y}"
    accent, accent_ink = _accent(summary)
    counts = summary["counts"]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=14 * mm,
        bottomMargin=20 * mm,
        title=f"Monthly Attendance — {month_label} — {employee.name}",
        author=organization.name,
        subject=f"Attendance statement for {employee.employee_code}",
    )
    width = doc.width

    period = f"{period_start:%d %b %Y} – {period_end:%d %b %Y}"
    if as_on and as_on < period_end:
        period += f" (as on {as_on:%d %b %Y})"

    story = [
        _banner(employee, organization, month_label, accent, width),
        Spacer(1, 14),
        Paragraph(f"Statement period: <b>{period}</b>", _lead),
        Spacer(1, 10),
        _stat_row(
            [
                (summary["working_days"], "Working days", "#111827", 15),
                (summary["payable_days"], "Payable days", "#111827", 15),
                (f"{summary['attendance_pct']}%", "Attendance", accent_ink, 15),
            ],
            width,
        ),
        Spacer(1, 6),
        _stat_row(
            [
                (counts[Attendance.PRESENT], "Present", "#16a34a", 13),
                (counts[Attendance.HALF_DAY], "Half day", "#d97706", 13),
                (counts[Attendance.ABSENT], "Absent", "#dc2626", 13),
                (counts[Attendance.LEAVE], "Leave", "#2563eb", 13),
            ],
            width,
        ),
        Spacer(1, 10),
    ]

    schedule = (
        f"Week offs <b>{counts[Attendance.WEEK_OFF]}</b> &middot; "
        f"Holidays <b>{counts[Attendance.HOLIDAY]}</b><br/>"
    )
    if summary["punctuality_pct"] is not None:
        schedule += (
            f"On time on <b>{summary['on_time_days']} of {summary['timed_days']}</b> days "
            f"({summary['punctuality_pct']}% punctual)"
        )
        if summary["late_days"]:
            schedule += (
                f', late <font color="#ea580c"><b>{summary["late_days"]}</b></font> '
                f"time{'s' if summary['late_days'] > 1 else ''}"
            )
        schedule += ".<br/>"
    schedule += (
        f"Schedule: <b>{escape(employee.working_days_display)}</b> "
        f"(weekly off {escape(employee.weekly_off_display)})."
    )
    story.append(Paragraph(schedule, _body))

    if summary["parties_count"]:
        story += [
            Spacer(1, 14),
            Paragraph("BUSINESS THIS MONTH", _section),
            _stat_row(
                [
                    (summary["orders_count"], "Orders", "#111827", 13),
                    (f"Rs {rupees(summary['order_total'])}", "Order value", "#111827", 13),
                    (f"Rs {rupees(summary['collected_total'])}", "Collected", "#16a34a", 13),
                ],
                width,
            ),
        ]

    story += [
        Spacer(1, 16),
        Paragraph("DAY BY DAY", _section),
        _day_table(records, width),
        Spacer(1, 14),
        Paragraph(
            f"This is an automated monthly statement from {escape(organization.name)}. "
            + (
                f"For corrections, write to {escape(organization.email)}."
                if organization.email
                else "Raise any correction with HR."
            ),
            _footnote,
        ),
    ]

    doc.build(story, onFirstPage=_page_furniture(organization, month_label),
              onLaterPages=_page_furniture(organization, month_label))
    return buffer.getvalue()


def monthly_pdf_filename(employee, period_start):
    """``Attendance-2026-08-EMP001.pdf`` — sorts by month in a mail client."""
    code = "".join(ch for ch in employee.employee_code if ch.isalnum()) or str(employee.pk)
    return f"Attendance-{period_start:%Y-%m}-{code}.pdf"
