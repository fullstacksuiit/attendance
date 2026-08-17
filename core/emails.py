"""Attendance notification emails.

Every send is recorded in :class:`~core.models.EmailLog`, so a failed SMTP
delivery never rolls back saved attendance — the admin sees which mails failed
and can retry them from the same screen.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Attendance, EmailLog, Holiday
from .pdf import build_monthly_pdf, monthly_pdf_filename

logger = logging.getLogger(__name__)

PRESENT_COLOR = "#16a34a"
LATE_COLOR = "#ea580c"
ABSENT_COLOR = "#dc2626"
HALF_COLOR = "#d97706"
LEAVE_COLOR = "#2563eb"

# Each accent carries a darker shade for the header gradient and a pale tint
# for callout blocks, so the whole mail reads as one colour family.
PALETTE = {
    PRESENT_COLOR: {"dark": "#15803d", "tint": "#ecfdf5", "line": "#bbf7d0"},
    LATE_COLOR: {"dark": "#c2410c", "tint": "#fff7ed", "line": "#fed7aa"},
    ABSENT_COLOR: {"dark": "#b91c1c", "tint": "#fef2f2", "line": "#fecaca"},
    HALF_COLOR: {"dark": "#b45309", "tint": "#fffbeb", "line": "#fde68a"},
    LEAVE_COLOR: {"dark": "#1d4ed8", "tint": "#eff6ff", "line": "#bfdbfe"},
}


def shades(accent):
    """Darker and pale companions for an accent colour."""
    return PALETTE.get(accent, PALETTE[PRESENT_COLOR])


def build_copy(attendance):
    """Headline, body copy and accent colour for one attendance record.

    The tone is a colleague's, not a warden's — good days get thanked, off days
    get stated plainly with an easy way to fix them.
    """
    organization = attendance.organization
    start = organization.work_start_time.strftime("%I:%M %p").lstrip("0")

    if attendance.status == Attendance.PRESENT:
        if attendance.is_late:
            return {
                "headline": "Present — Late Arrival",
                "message": (
                    "Thanks for coming in — your day is marked present. Your "
                    f"check-in came {attendance.late_by_display} past the {start} "
                    "start time, so it goes on record as a late arrival. If "
                    "something held you up, just tell us and we will note it. "
                    "Otherwise, an early start tomorrow keeps your month clean."
                ),
                "accent": LATE_COLOR,
                "icon": "⏰",
                "tagline": "Marked present, clocked in a little late",
                "subject_phrase": f"Late by {attendance.late_by_display}",
            }
        return {
            "headline": "Present — On Time",
            "message": (
                "Marked present and on time — thank you. Showing up like this, "
                "day after day, is exactly what lets the whole team plan around "
                "you. Have a great day ahead."
            ),
            "accent": PRESENT_COLOR,
            "icon": "✅",
            "tagline": "You were at your desk on time today",
            "subject_phrase": "Present",
        }

    if attendance.status == Attendance.HALF_DAY:
        return {
            "headline": "Marked Half Day",
            "message": (
                "Your half day is recorded, and half of today counts as payable. "
                "Hope everything is sorted at your end — we will see you for a "
                "full day tomorrow."
            ),
            "accent": HALF_COLOR,
            "icon": "🌗",
            "tagline": "Half of today counts towards your payable days",
            "subject_phrase": "Half day",
        }

    if attendance.status == Attendance.ABSENT:
        return {
            "headline": "Marked Absent",
            "message": (
                "We did not receive an attendance entry for you today, so the day "
                "is marked absent. If you were working, on leave, or this is "
                "simply a mistake, reply to this mail or speak to HR and we will "
                "correct it the same day. Hope all is well — see you tomorrow."
            ),
            "accent": ABSENT_COLOR,
            "icon": "📄",
            "tagline": "No attendance entry reached us for today",
            "subject_phrase": "Absent",
        }

    return {
        "headline": "Marked on Leave",
        "message": (
            "Your leave for today is recorded — enjoy the break. Switch off "
            "properly; the team has the day covered, and you will be far more "
            "use to everyone rested than half-available."
        ),
        "accent": LEAVE_COLOR,
        "icon": "🌴",
        "tagline": "Your leave for today is on record",
        "subject_phrase": "On leave",
    }


def build_highlight(attendance, summary, streak):
    """One motivating (or sobering) line drawn from the employee's own record."""
    # A good day in the field outranks the attendance stats — lead with it.
    if attendance.total_collected > 0:
        collected = f"{attendance.total_collected:,.0f}"
        if attendance.parties_count > 1:
            return (
                f"Rs {collected} collected from {attendance.parties_count} parties today — "
                "well done. That is the number that moves the month."
            )
        return f"Rs {collected} collected today — nice work. Keep the follow-ups going."

    if attendance.orders_count > 0:
        return (
            f"Orders from {attendance.orders_count} "
            f"customer{'s' if attendance.orders_count > 1 else ''} booked today — "
            "good going. Closing the collection on them is the next win."
        )

    if attendance.status == Attendance.PRESENT and not attendance.is_late:
        if streak >= 5:
            return f"{streak} days present in a row — a streak worth protecting."
        if summary["punctuality_pct"] == 100 and summary["timed_days"] >= 3:
            return f"Perfect punctuality this month — {summary['timed_days']}/{summary['timed_days']} days on time."

    if attendance.is_late and summary["late_days"] > 1:
        return (
            f"That makes {summary['late_days']} late arrivals this month. "
            "A couple of early starts and you will pull it right back."
        )

    if summary["attendance_pct"] >= 90 and summary["marked_days"] >= 5:
        return f"You are at {summary['attendance_pct']}% attendance this month — a strong record. Hold it."

    return ""


def build_message(attendance, connection=None, previous_status=None):
    """Render one attendance mail. Returns (message, subject).

    ``previous_status`` marks the mail as a correction of an earlier one, which
    changes the subject prefix and adds a "changed from X to Y" line.
    """
    employee = attendance.employee
    organization = attendance.organization
    copy = build_copy(attendance)
    summary = employee.month_summary(attendance.date)
    streak = employee.present_streak(attendance.date)

    phrase = copy["subject_phrase"]
    if attendance.check_in_time and not attendance.is_late:
        phrase = f"{phrase} (in {attendance.check_in_time.strftime('%I:%M %p').lstrip('0')})"

    prefix = f"[{organization.name}]"
    if previous_status:
        prefix = f"{prefix} Updated ·"

    subject = (
        f"{prefix} {attendance.date:%d %b %Y} — "
        f"{employee.name} ({employee.employee_code}): {phrase}"
    )

    previous_label = dict(Attendance.STATUS_CHOICES).get(previous_status) if previous_status else ""

    context = {
        "attendance": attendance,
        "employee": employee,
        "organization": organization,
        "status_label": attendance.get_status_display(),
        "headline": copy["headline"],
        "body_message": copy["message"],
        "accent": copy["accent"],
        "accent_dark": shades(copy["accent"])["dark"],
        "accent_tint": shades(copy["accent"])["tint"],
        "accent_line": shades(copy["accent"])["line"],
        "icon": copy["icon"],
        "tagline": copy["tagline"],
        "summary": summary,
        "streak": streak,
        "highlight": build_highlight(attendance, summary, streak),
        "work_start": organization.work_start_time,
        "previous_label": previous_label,
        "party_lines": attendance.party_lines,
    }
    html_body = render_to_string("emails/attendance_notification.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(html_body.replace("</p>", "</p>\n").replace("<br>", "\n")),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[employee.email],
        reply_to=[organization.email] if organization.email else None,
        connection=connection,
    )
    message.attach_alternative(html_body, "text/html")
    return message, subject


def build_monthly_grade(summary, is_running_month=False):
    """Badge, opening line and accent for a month, keyed off attendance.

    A month still running is never graded: ``attendance_pct`` measures payable
    days against the *whole* month's working days, so a mid-month statement
    would read as a poor record no matter how the employee is doing.
    """
    if is_running_month:
        return {
            "badge": "Month in progress",
            "icon": "📅",
            "note": "This is your record up to today — the month is still running.",
            "accent": PRESENT_COLOR,
        }

    pct = summary["attendance_pct"]
    if pct >= 95:
        return {
            "badge": "Outstanding month",
            "icon": "🏆",
            "note": "An almost perfect record — thank you for the consistency.",
            "accent": PRESENT_COLOR,
        }
    if pct >= 85:
        return {
            "badge": "Strong month",
            "icon": "⭐",
            "note": "A solid, dependable month. Nicely done.",
            "accent": PRESENT_COLOR,
        }
    if pct >= 75:
        return {
            "badge": "Good month",
            "icon": "👍",
            "note": "A steady month — a few more full days and it is a great one.",
            "accent": PRESENT_COLOR,
        }
    return {
        "badge": "Room to build on",
        "icon": "📈",
        "note": "A lighter month than usual. Next month is a clean slate — let us make it count.",
        "accent": LATE_COLOR,
    }


def build_monthly_message(employee, period_start, period_end, summary, records, connection=None):
    """Render one employee's month-end statement. Returns (message, subject).

    The same statement rides along as a PDF the employee can file or print. A
    rendering failure costs the attachment, never the mail — the body already
    carries every number.
    """
    organization = employee.organization
    is_running_month = period_end >= timezone.localdate()

    subject = (
        f"[{organization.name}] Monthly Attendance — {period_start:%B %Y} — "
        f"{employee.name} ({employee.employee_code})"
    )

    try:
        pdf_bytes = build_monthly_pdf(
            employee, period_start, period_end, summary, records, as_on=timezone.localdate()
        )
    except Exception:
        logger.exception("Monthly PDF failed for %s — sending mail without it", employee.email)
        pdf_bytes = None

    grade = build_monthly_grade(summary, is_running_month)
    context = {
        "employee": employee,
        "organization": organization,
        "summary": summary,
        "records": records,
        "period_start": period_start,
        "period_end": period_end,
        "month_label": f"{period_start:%B %Y}",
        "is_running_month": is_running_month,
        "as_on": timezone.localdate(),
        "accent": grade["accent"],
        "accent_dark": shades(grade["accent"])["dark"],
        "accent_tint": shades(grade["accent"])["tint"],
        "accent_line": shades(grade["accent"])["line"],
        "grade_badge": grade["badge"],
        "grade_icon": grade["icon"],
        "grade_note": grade["note"],
        "pdf_attached": pdf_bytes is not None,
    }
    html_body = render_to_string("emails/monthly_report.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(html_body.replace("</p>", "</p>\n").replace("<br>", "\n")),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[employee.email],
        reply_to=[organization.email] if organization.email else None,
        connection=connection,
    )
    message.attach_alternative(html_body, "text/html")
    if pdf_bytes:
        message.attach(
            monthly_pdf_filename(employee, period_start), pdf_bytes, "application/pdf"
        )
    return message, subject


def send_monthly_reports(organization, period_start, period_end, employees):
    """Mail each employee their own summary for the period.

    Employees with no attendance in the period are skipped rather than sent an
    empty statement. Returns ``(sent, failed, skipped, failures)``.
    """
    holiday_dates = list(
        Holiday.objects.filter(
            organization=organization, date__range=(period_start, period_end)
        ).values_list("date", flat=True)
    )

    sent = 0
    skipped = 0
    failures = []
    logs = []

    connection = get_connection(fail_silently=False)
    try:
        connection.open()
    except Exception as exc:  # pragma: no cover - depends on SMTP availability
        logger.exception("Could not open email connection for monthly reports")
        return 0, len(employees), 0, [(e.name, str(exc)) for e in employees]

    try:
        for employee in employees:
            records = list(
                employee.attendance_records.filter(date__range=(period_start, period_end))
                .order_by("date")
                .prefetch_related("collections")
            )
            if not records:
                skipped += 1
                continue
            for record in records:
                record.organization = organization  # prime FK cache for is_late

            summary = employee.month_summary(period_start, holiday_dates)
            message, subject = build_monthly_message(
                employee, period_start, period_end, summary, records, connection=connection
            )
            try:
                message.send(fail_silently=False)
            except Exception as exc:
                logger.warning("Monthly report failed for %s: %s", employee.email, exc)
                failures.append((employee.name, str(exc)))
                logs.append(
                    EmailLog(
                        organization=organization,
                        employee=employee,
                        to_email=employee.email,
                        subject=subject,
                        status=EmailLog.FAILED,
                        error=str(exc),
                    )
                )
            else:
                sent += 1
                logs.append(
                    EmailLog(
                        organization=organization,
                        employee=employee,
                        to_email=employee.email,
                        subject=subject,
                        status=EmailLog.SENT,
                    )
                )
    finally:
        connection.close()

    EmailLog.objects.bulk_create(logs)
    return sent, len(failures), skipped, failures


def send_attendance_emails(attendance_records, previous_statuses=None):
    """Send one mail per record over a single SMTP connection.

    ``previous_statuses`` maps attendance id -> the status it held before this
    save, for records that are corrections rather than first notices.

    Returns ``(sent_count, failed_count, failures)`` where ``failures`` is a
    list of ``(employee_name, error)`` tuples.
    """
    previous_statuses = previous_statuses or {}
    records = list(attendance_records)
    if not records:
        return 0, 0, []

    sent = 0
    failures = []
    logs = []

    connection = get_connection(fail_silently=False)
    try:
        connection.open()
    except Exception as exc:  # pragma: no cover - depends on SMTP availability
        logger.exception("Could not open email connection")
        for record in records:
            failures.append((record.employee.name, str(exc)))
            logs.append(
                EmailLog(
                    organization=record.organization,
                    employee=record.employee,
                    attendance=record,
                    to_email=record.employee.email,
                    subject="(connection failed)",
                    status=EmailLog.FAILED,
                    error=str(exc),
                )
            )
        EmailLog.objects.bulk_create(logs)
        return 0, len(records), failures

    try:
        for record in records:
            message, subject = build_message(
                record,
                connection=connection,
                previous_status=previous_statuses.get(record.id),
            )
            try:
                message.send(fail_silently=False)
            except Exception as exc:
                logger.warning("Attendance mail failed for %s: %s", record.employee.email, exc)
                failures.append((record.employee.name, str(exc)))
                logs.append(
                    EmailLog(
                        organization=record.organization,
                        employee=record.employee,
                        attendance=record,
                        to_email=record.employee.email,
                        subject=subject,
                        status=EmailLog.FAILED,
                        error=str(exc),
                    )
                )
            else:
                sent += 1
                logs.append(
                    EmailLog(
                        organization=record.organization,
                        employee=record.employee,
                        attendance=record,
                        to_email=record.employee.email,
                        subject=subject,
                        status=EmailLog.SENT,
                        # Stamped so a later save can tell whether this employee
                        # already holds a mail with these exact details.
                        content_hash=record.content_hash,
                    )
                )
    finally:
        connection.close()

    EmailLog.objects.bulk_create(logs)
    return sent, len(failures), failures
