"""Attendance notification emails.

Every send is recorded in :class:`~core.models.EmailLog`, so a failed SMTP
delivery never rolls back saved attendance — the admin sees which mails failed
and can retry them from the same screen.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from .models import Attendance, EmailLog

logger = logging.getLogger(__name__)

PRESENT_COLOR = "#16a34a"
LATE_COLOR = "#ea580c"
ABSENT_COLOR = "#dc2626"
HALF_COLOR = "#d97706"
LEAVE_COLOR = "#2563eb"


def build_copy(attendance):
    """Headline, body copy and accent colour for one attendance record.

    Punctual days get recognition, late arrivals get a direct account of the
    cost — both backed by the employee's own numbers rather than adjectives.
    """
    organization = attendance.organization
    start = organization.work_start_time.strftime("%I:%M %p").lstrip("0")

    if attendance.status == Attendance.PRESENT:
        if attendance.is_late:
            return {
                "headline": "Present — Late Arrival",
                "message": (
                    f"You were marked present, but you clocked in "
                    f"{attendance.late_by_display} past the {start} start time. "
                    "Work that waits on you lands on colleagues who were at their "
                    "desk on time, and every late arrival stays on your record for "
                    "the month. Please start on time tomorrow."
                ),
                "accent": LATE_COLOR,
                "subject_phrase": f"Late by {attendance.late_by_display}",
            }
        return {
            "headline": "Present — On Time",
            "message": (
                "Marked present and on time. Turning up like this, day after day, "
                "is what lets the whole team plan around you. Keep it going."
            ),
            "accent": PRESENT_COLOR,
            "subject_phrase": "Present",
        }

    if attendance.status == Attendance.HALF_DAY:
        return {
            "headline": "Marked Half Day",
            "message": (
                "Half of today is counted as payable. If you can shift the other "
                "half to a full day tomorrow, your month-end numbers will thank you."
            ),
            "accent": HALF_COLOR,
            "subject_phrase": "Half day",
        }

    if attendance.status == Attendance.ABSENT:
        return {
            "headline": "Marked Absent",
            "message": (
                "No attendance was recorded for you today. Unplanned absences cut "
                "directly into your payable days and are visible in your monthly "
                "record. If this is a mistake, raise it with Consent Person today — otherwise "
                "we will see you back tomorrow."
            ),
            "accent": ABSENT_COLOR,
            "subject_phrase": "Absent",
        }

    return {
        "headline": "Marked on Leave",
        "message": (
            "Your leave for today is recorded. Switch off properly — you will be "
            "more use to the team rested than half-available."
        ),
        "accent": LEAVE_COLOR,
        "subject_phrase": "On leave",
    }


def build_highlight(attendance, summary, streak):
    """One motivating (or sobering) line drawn from the employee's own record."""
    # A good day in the field outranks the attendance stats — lead with it.
    if attendance.total_collected > 0:
        collected = f"{attendance.total_collected:,.0f}"
        if attendance.parties_count > 1:
            return (
                f"Rs {collected} collected from {attendance.parties_count} parties today. "
                "That is the number that moves the month."
            )
        return f"Rs {collected} collected today. Keep the follow-ups going."

    if attendance.orders_count > 0:
        return (
            f"Orders from {attendance.orders_count} "
            f"customer{'s' if attendance.orders_count > 1 else ''} booked today — "
            "now close the collection on them."
        )

    if attendance.status == Attendance.PRESENT and not attendance.is_late:
        if streak >= 5:
            return f"{streak} days present in a row. That is a streak worth protecting."
        if summary["punctuality_pct"] == 100 and summary["timed_days"] >= 3:
            return f"Perfect punctuality this month — {summary['timed_days']}/{summary['timed_days']} days on time."

    if attendance.is_late and summary["late_days"] > 1:
        return (
            f"That is {summary['late_days']} late arrivals this month. "
            "Two more and it stops looking like traffic."
        )

    if summary["attendance_pct"] >= 90 and summary["marked_days"] >= 5:
        return f"You are at {summary['attendance_pct']}% attendance this month. Strong record — hold it."

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
                    )
                )
    finally:
        connection.close()

    EmailLog.objects.bulk_create(logs)
    return sent, len(failures), failures
