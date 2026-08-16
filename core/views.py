import calendar
import csv
import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .emails import send_attendance_emails, send_monthly_reports
from .forms import EmployeeForm, HolidayForm, OrganizationForm, OrganizationSignupForm
from .models import Attendance, Collection, EmailLog, Employee, Holiday

VALID_STATUSES = {choice for choice, _ in Attendance.STATUS_CHOICES}

#: Guard against a runaway payload; no field round realistically exceeds this.
MAX_PARTIES_PER_DAY = 100


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def org_required(view):
    """Login + an organization on the user. Platform superusers have none."""

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.user.organization_id is None:
            messages.error(request, "Your account is not linked to any organization.")
            return redirect("login")
        return view(request, *args, **kwargs)

    return wrapper


def parse_date(value, fallback=None):
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    return fallback or timezone.localdate()


def parse_amount(value):
    """Money from JSON. Blank/garbage becomes 0; negatives are clamped away."""
    if value in (None, ""):
        return Decimal("0")
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if amount.is_nan() or amount.is_infinite() or amount < 0:
        return Decimal("0")
    return amount.quantize(Decimal("0.01"))


def parse_collections(entries):
    """Clean the party-wise lines posted with one employee's row.

    A line needs a party name and at least one non-zero amount to be worth
    storing — blank rows the admin left behind are dropped.
    """
    cleaned = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        party = (entry.get("party_name") or "").strip()[:150]
        order_amount = parse_amount(entry.get("order_amount"))
        collected_amount = parse_amount(entry.get("collected_amount"))
        if not party or (order_amount == 0 and collected_amount == 0):
            continue
        cleaned.append(
            {
                "party_name": party,
                "order_amount": order_amount,
                "collected_amount": collected_amount,
            }
        )
        if len(cleaned) >= MAX_PARTIES_PER_DAY:
            break
    return cleaned


def collection_signature(lines):
    """Comparable snapshot of a day's party lines, for change detection."""
    return sorted(
        (line["party_name"], line["order_amount"], line["collected_amount"])
        if isinstance(line, dict)
        else (line.party_name, line.order_amount, line.collected_amount)
        for line in lines
    )


def parse_time(value):
    """Accept the "HH:MM" (or "HH:MM:SS") that <input type=time> submits."""
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def month_bounds(year, month):
    first = date_cls(year, month, 1)
    last = date_cls(year, month, calendar.monthrange(year, month)[1])
    return first, last


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = OrganizationSignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(
            request, f"Welcome, {user.get_short_name()}! Add your employees to get started."
        )
        return redirect("employee_create")

    return render(request, "signup.html", {"form": form})


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@org_required
def dashboard(request):
    org = request.user.organization
    today = timezone.localdate()
    selected_date = parse_date(request.GET.get("date"), today)

    employees = Employee.objects.filter(organization=org)
    active_count = employees.filter(is_active=True).count()

    counts = dict(
        Attendance.objects.filter(organization=org, date=selected_date)
        .values_list("status")
        .annotate(total=Count("id"))
    )
    marked = sum(counts.values())

    month_start, month_end = month_bounds(today.year, today.month)
    month_counts = dict(
        Attendance.objects.filter(organization=org, date__range=(month_start, month_end))
        .values_list("status")
        .annotate(total=Count("id"))
    )

    day_business = Collection.objects.filter(
        attendance__organization=org, attendance__date=selected_date
    ).aggregate(
        orders=Count("id", filter=Q(order_amount__gt=0)),
        order_total=Sum("order_amount"),
        collected_total=Sum("collected_amount"),
    )
    month_business = Collection.objects.filter(
        attendance__organization=org, attendance__date__range=(month_start, month_end)
    ).aggregate(
        orders=Count("id", filter=Q(order_amount__gt=0)),
        order_total=Sum("order_amount"),
        collected_total=Sum("collected_amount"),
    )

    recent_emails = (
        EmailLog.objects.filter(organization=org)
        .select_related("employee")[:8]
    )
    failed_today = EmailLog.objects.filter(
        organization=org, status=EmailLog.FAILED, created_at__date=today
    ).count()

    context = {
        "selected_date": selected_date,
        "today": today,
        "total_employees": employees.count(),
        "active_employees": active_count,
        "marked_today": marked,
        "pending_today": max(active_count - marked, 0),
        "present_count": counts.get(Attendance.PRESENT, 0),
        "absent_count": counts.get(Attendance.ABSENT, 0),
        "half_day_count": counts.get(Attendance.HALF_DAY, 0),
        "leave_count": counts.get(Attendance.LEAVE, 0),
        "month_present": month_counts.get(Attendance.PRESENT, 0),
        "month_absent": month_counts.get(Attendance.ABSENT, 0),
        "month_half_day": month_counts.get(Attendance.HALF_DAY, 0),
        "month_leave": month_counts.get(Attendance.LEAVE, 0),
        "month_label": f"{calendar.month_name[today.month]} {today.year}",
        "recent_emails": recent_emails,
        "failed_today": failed_today,
        "day_orders": day_business["orders"] or 0,
        "day_order_total": day_business["order_total"] or Decimal("0"),
        "day_collected": day_business["collected_total"] or Decimal("0"),
        "month_orders": month_business["orders"] or 0,
        "month_order_total": month_business["order_total"] or Decimal("0"),
        "month_collected": month_business["collected_total"] or Decimal("0"),
    }
    return render(request, "dashboard.html", context)


# --------------------------------------------------------------------------
# employees
# --------------------------------------------------------------------------


@org_required
def employee_list(request):
    org = request.user.organization
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")

    employees = Employee.objects.filter(organization=org)
    if query:
        employees = employees.filter(
            Q(name__icontains=query)
            | Q(email__icontains=query)
            | Q(employee_code__icontains=query)
            | Q(department__icontains=query)
            | Q(designation__icontains=query)
        )
    if status == "active":
        employees = employees.filter(is_active=True)
    elif status == "inactive":
        employees = employees.filter(is_active=False)

    return render(
        request,
        "employees/list.html",
        {"employees": employees, "query": query, "status": status},
    )


@org_required
def employee_create(request):
    org = request.user.organization
    form = EmployeeForm(request.POST or None, organization=org)
    if request.method == "POST" and form.is_valid():
        employee = form.save()
        messages.success(request, f"{employee.name} added.")
        if "save_and_add" in request.POST:
            return redirect("employee_create")
        return redirect("employee_list")

    return render(request, "employees/form.html", {"form": form, "is_edit": False})


@org_required
def employee_edit(request, pk):
    org = request.user.organization
    employee = get_object_or_404(Employee, pk=pk, organization=org)
    form = EmployeeForm(request.POST or None, instance=employee, organization=org)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{employee.name} updated.")
        return redirect("employee_list")

    return render(
        request, "employees/form.html", {"form": form, "is_edit": True, "employee": employee}
    )


@org_required
@require_POST
def employee_delete(request, pk):
    org = request.user.organization
    employee = get_object_or_404(Employee, pk=pk, organization=org)
    name = employee.name
    employee.delete()
    messages.success(request, f"{name} and their attendance history were deleted.")
    return redirect("employee_list")


@org_required
@require_POST
def employee_toggle_active(request, pk):
    org = request.user.organization
    employee = get_object_or_404(Employee, pk=pk, organization=org)
    employee.is_active = not employee.is_active
    employee.save(update_fields=["is_active"])
    state = "activated" if employee.is_active else "deactivated"
    messages.success(request, f"{employee.name} {state}.")
    return redirect(request.META.get("HTTP_REFERER") or "employee_list")


@org_required
def employee_detail(request, pk):
    org = request.user.organization
    employee = get_object_or_404(Employee, pk=pk, organization=org)
    today = timezone.localdate()
    year = int(request.GET.get("year", today.year))
    month = int(request.GET.get("month", today.month))
    start, end = month_bounds(year, month)

    records = list(
        employee.attendance_records.filter(date__range=(start, end))
        .order_by("date")
        .prefetch_related("collections")
    )
    summary = {status: 0 for status, _ in Attendance.STATUS_CHOICES}
    for record in records:
        record.organization = org  # prime the FK cache; is_late would query per row
        summary[record.status] += 1
    payable = sum(record.day_value for record in records)
    late_days = sum(1 for record in records if record.is_late)
    business = employee.month_summary(start)

    return render(
        request,
        "employees/detail.html",
        {
            "employee": employee,
            "records": records,
            "summary": summary,
            "payable_days": payable,
            "late_days": late_days,
            "business": business,
            "month_label": f"{calendar.month_name[month]} {year}",
            "year": year,
            "month": month,
            "months": list(enumerate(calendar.month_name))[1:],
            "years": range(today.year - 3, today.year + 1),
        },
    )


# --------------------------------------------------------------------------
# attendance
# --------------------------------------------------------------------------


@org_required
def attendance_sheet(request):
    org = request.user.organization
    today = timezone.localdate()
    selected_date = parse_date(request.GET.get("date"), today)

    employees = Employee.objects.filter(organization=org, is_active=True)
    existing = {}
    for record in Attendance.objects.filter(
        organization=org, date=selected_date, employee__in=employees
    ).prefetch_related("collections"):
        record.organization = org
        existing[record.employee_id] = record

    holiday = Holiday.objects.filter(organization=org, date=selected_date).first()

    rows = []
    for employee in employees:
        record = existing.get(employee.id)
        # What this day is for this employee when nobody touches the row.
        if holiday:
            default_status = Attendance.HOLIDAY
        elif not employee.works_on(selected_date):
            default_status = Attendance.WEEK_OFF
        else:
            default_status = ""
        rows.append(
            {
                "employee": employee,
                "status": record.status if record else "",
                "default_status": default_status,
                "off_label": (
                    f"Holiday · {holiday.name}"
                    if holiday
                    else ("Week Off" if default_status == Attendance.WEEK_OFF else "")
                ),
                "note": record.note if record else "",
                "check_in_time": record.check_in_time if record else None,
                "check_out_time": record.check_out_time if record else None,
                "is_late": record.is_late if record else False,
                "saved": record is not None,
                # Serialised for the JS that rebuilds the party-line editor.
                "collections_json": json.dumps(
                    [
                        {
                            "party_name": line.party_name,
                            "order_amount": str(line.order_amount),
                            "collected_amount": str(line.collected_amount),
                        }
                        for line in (record.party_lines if record else [])
                    ]
                ),
            }
        )

    return render(
        request,
        "attendance/sheet.html",
        {
            "rows": rows,
            "selected_date": selected_date,
            "is_future": selected_date > today,
            "already_marked": len(existing),
            "holiday": holiday,
            "off_count": sum(1 for row in rows if row["default_status"]),
            "status_choices": [
                (value, label)
                for value, label in Attendance.STATUS_CHOICES
                if value in Attendance.MARKABLE_STATUSES
            ],
            "max_date": today,
            "prev_date": selected_date - timedelta(days=1),
            "next_date": selected_date + timedelta(days=1),
            "work_start_time": org.work_start_time,
            "grace_period": org.grace_period_minutes,
        },
    )


@org_required
@require_POST
def attendance_save(request):
    """Save the day's sheet and email each affected employee.

    Emails are sent after the DB transaction commits, so a mail failure never
    discards saved attendance — failures come back in the JSON response.
    """
    org = request.user.organization
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid request body."}, status=400)

    selected_date = parse_date(payload.get("date"))
    if selected_date > timezone.localdate():
        return JsonResponse(
            {"ok": False, "error": "Attendance cannot be marked for a future date."}, status=400
        )

    entries = payload.get("records") or []
    notify = bool(payload.get("notify", True))

    cleaned = []
    for entry in entries:
        status = (entry.get("status") or "").strip()
        if status not in VALID_STATUSES:
            continue
        try:
            employee_id = int(entry.get("employee_id"))
        except (TypeError, ValueError):
            continue
        # Times and business only make sense for a day they actually turned up.
        attended = status in (Attendance.PRESENT, Attendance.HALF_DAY)
        cleaned.append(
            {
                "employee_id": employee_id,
                "status": status,
                "note": (entry.get("note") or "").strip()[:255],
                "check_in_time": parse_time(entry.get("check_in_time")) if attended else None,
                "check_out_time": parse_time(entry.get("check_out_time")) if attended else None,
                # An absent employee books no orders, so their lines are dropped.
                "collections": parse_collections(entry.get("collections")) if attended else [],
            }
        )

    if not cleaned:
        return JsonResponse(
            {"ok": False, "error": "Select a status for at least one employee."}, status=400
        )

    employees = {
        employee.id: employee
        for employee in Employee.objects.filter(
            organization=org, id__in=[item["employee_id"] for item in cleaned]
        )
    }

    previous = {}
    for record in Attendance.objects.filter(
        organization=org, date=selected_date, employee_id__in=employees
    ).prefetch_related("collections"):
        record.organization = org  # prime the FK cache so is_late costs no query
        previous[record.employee_id] = record

    saved_ids = []
    notify_ids = []
    unchanged_ids = []
    previous_statuses = {}

    with transaction.atomic():
        for item in cleaned:
            employee = employees.get(item["employee_id"])
            if employee is None:
                continue  # not this org's employee — ignore silently

            before = previous.get(employee.id)
            before_status = before.status if before else None
            before_late = before.is_late if before else False
            before_business = collection_signature(before.party_lines) if before else []

            record, created = Attendance.objects.update_or_create(
                organization=org,
                employee=employee,
                date=selected_date,
                defaults={
                    "status": item["status"],
                    "note": item["note"],
                    "check_in_time": item["check_in_time"],
                    "check_out_time": item["check_out_time"],
                    "marked_by": request.user,
                },
            )
            record.organization = org
            saved_ids.append(record.id)

            # The posted lines replace the day's lines outright, so an edited or
            # removed party is reflected instead of accumulating duplicates.
            record.collections.all().delete()
            if item["collections"]:
                Collection.objects.bulk_create(
                    [Collection(attendance=record, **line) for line in item["collections"]]
                )
            after_business = collection_signature(item["collections"])

            # Only a real change is worth another mail. Re-saving an identical
            # sheet must stay silent, or employees get the same notice twice.
            if (
                created
                or before_status != record.status
                or before_late != record.is_late
                or before_business != after_business
            ):
                notify_ids.append(record.id)
                if before_status:
                    previous_statuses[record.id] = before_status
            else:
                unchanged_ids.append(record.id)

    if not saved_ids:
        return JsonResponse({"ok": False, "error": "No valid employees in request."}, status=400)

    result = {
        "ok": True,
        "saved": len(saved_ids),
        "emailed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
    }

    if notify:
        # An unchanged row whose mail never actually landed still deserves one.
        if unchanged_ids:
            delivered = set(
                EmailLog.objects.filter(
                    attendance_id__in=unchanged_ids, status=EmailLog.SENT
                ).values_list("attendance_id", flat=True)
            )
            notify_ids.extend(pk for pk in unchanged_ids if pk not in delivered)

        # Nobody needs a mail telling them Sunday was Sunday.
        records = [
            record
            for record in Attendance.objects.filter(id__in=notify_ids)
            .select_related("employee", "organization")
            .prefetch_related("collections")
            if record.status not in Attendance.NON_WORKING_STATUSES
        ]
        result["skipped"] = len(saved_ids) - len(records)

        sent, failed, failures = send_attendance_emails(records, previous_statuses)
        result.update(
            {
                "emailed": sent,
                "failed": failed,
                "failures": [{"name": name, "error": error} for name, error in failures],
            }
        )

    if not notify:
        result["message"] = f"Saved {result['saved']} record(s). No mails sent."
    else:
        message = f"Saved {result['saved']} record(s), emailed {result['emailed']}."
        if result["skipped"]:
            message += f" {result['skipped']} not mailed (unchanged, week off or holiday)."
        result["message"] = message

    return JsonResponse(result)


@org_required
@require_POST
def attendance_resend(request):
    """Retry notification mails for a given date."""
    org = request.user.organization
    selected_date = parse_date(request.POST.get("date"))
    records = [
        record
        for record in Attendance.objects.filter(organization=org, date=selected_date)
        .select_related("employee", "organization")
        .prefetch_related("collections")
        if record.status not in Attendance.NON_WORKING_STATUSES
    ]

    if not records:
        messages.warning(
            request, f"Nothing to mail for {selected_date:%d %b %Y}."
        )
        return redirect(f"/attendance/?date={selected_date:%Y-%m-%d}")

    sent, failed, _ = send_attendance_emails(records)
    if failed:
        messages.warning(request, f"Re-sent {sent} mail(s), {failed} failed. Check email logs.")
    else:
        messages.success(request, f"Re-sent {sent} attendance mail(s).")
    return redirect(f"/attendance/?date={selected_date:%Y-%m-%d}")


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


@org_required
def report_monthly(request):
    org = request.user.organization
    today = timezone.localdate()
    year = int(request.GET.get("year", today.year))
    month = int(request.GET.get("month", today.month))
    start, end = month_bounds(year, month)

    employees = Employee.objects.filter(organization=org)
    records = Attendance.objects.filter(organization=org, date__range=(start, end))

    business = {
        row["attendance__employee"]: row
        for row in Collection.objects.filter(
            attendance__organization=org, attendance__date__range=(start, end)
        )
        .values("attendance__employee")
        .annotate(
            orders=Count("id", filter=Q(order_amount__gt=0)),
            order_total=Sum("order_amount"),
            collected_total=Sum("collected_amount"),
        )
    }

    by_employee = {
        employee.id: {"employee": employee, "counts": {}, "payable": 0.0, "late": 0}
        for employee in employees
    }
    for record in records:
        row = by_employee.get(record.employee_id)
        if row is None:
            continue
        record.organization = org  # prime the FK cache; is_late would query per row
        row["counts"][record.status] = row["counts"].get(record.status, 0) + 1
        row["payable"] += record.day_value
        if record.is_late:
            row["late"] += 1

    holiday_dates = list(
        Holiday.objects.filter(organization=org, date__range=(start, end)).values_list(
            "date", flat=True
        )
    )

    rows = []
    for row in by_employee.values():
        sales = business.get(row["employee"].id, {})
        scheduled = row["employee"].working_days_between(start, end, holiday_dates)
        payable = round(row["payable"], 1)
        rows.append(
            {
                "employee": row["employee"],
                "working_days": scheduled,
                "attendance_pct": round(min(payable / scheduled, 1) * 100) if scheduled else 0,
                "present": row["counts"].get(Attendance.PRESENT, 0),
                "absent": row["counts"].get(Attendance.ABSENT, 0),
                "half_day": row["counts"].get(Attendance.HALF_DAY, 0),
                "leave": row["counts"].get(Attendance.LEAVE, 0),
                "late": row["late"],
                "payable": payable,
                "orders": sales.get("orders") or 0,
                "order_total": sales.get("order_total") or Decimal("0"),
                "collected": sales.get("collected_total") or Decimal("0"),
            }
        )
    rows.sort(key=lambda item: item["employee"].name)

    totals = {
        "orders": sum(row["orders"] for row in rows),
        "order_total": sum((row["order_total"] for row in rows), Decimal("0")),
        "collected": sum((row["collected"] for row in rows), Decimal("0")),
    }

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="attendance-{year}-{month:02d}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Employee Code", "Name", "Email", "Working Days", "Present", "Absent",
                "Half Day", "Leave", "Late Arrivals", "Payable Days", "Attendance %",
                "Orders", "Order Value", "Collected",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["employee"].employee_code,
                    row["employee"].name,
                    row["employee"].email,
                    row["working_days"],
                    row["present"],
                    row["absent"],
                    row["half_day"],
                    row["leave"],
                    row["late"],
                    row["payable"],
                    row["attendance_pct"],
                    row["orders"],
                    # Formatted so the column stays 2-decimal money regardless
                    # of what the database backend hands back for SUM().
                    f"{row['order_total']:.2f}",
                    f"{row['collected']:.2f}",
                ]
            )
        writer.writerow(
            ["", "TOTAL", "", "", "", "", "", "", "", "", "",
             totals["orders"], f"{totals['order_total']:.2f}", f"{totals['collected']:.2f}"]
        )
        return response

    return render(
        request,
        "reports/monthly.html",
        {
            "rows": rows,
            "year": year,
            "month": month,
            "month_label": f"{calendar.month_name[month]} {year}",
            "months": list(enumerate(calendar.month_name))[1:],
            "years": range(today.year - 3, today.year + 1),
            "calendar_days": (end - start).days + 1,
            "holiday_count": len(holiday_dates),
            "totals": totals,
        },
    )


@org_required
@require_POST
def report_send(request):
    """Mail every employee their own summary for the selected month."""
    org = request.user.organization
    today = timezone.localdate()
    try:
        year = int(request.POST.get("year", today.year))
        month = int(request.POST.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month

    if not 1 <= month <= 12:
        messages.error(request, "Pick a valid month.")
        return redirect("report_monthly")

    start, end = month_bounds(year, month)
    if start > today:
        messages.warning(request, "That month has not started yet.")
        return redirect(f"/reports/monthly/?year={year}&month={month}")

    employees = list(Employee.objects.filter(organization=org, is_active=True))
    sent, failed, skipped, failures = send_monthly_reports(org, start, end, employees)

    label = f"{calendar.month_name[month]} {year}"
    if failed:
        names = ", ".join(name for name, _ in failures[:3])
        messages.warning(
            request,
            f"{label}: emailed {sent}, {failed} failed ({names}…). See the email log.",
        )
    elif sent:
        note = f" {skipped} skipped (no attendance recorded)." if skipped else ""
        messages.success(request, f"{label} report emailed to {sent} employee(s).{note}")
    else:
        messages.warning(request, f"No attendance recorded in {label} — nothing to send.")

    return redirect(f"/reports/monthly/?year={year}&month={month}")


@org_required
@require_POST
def employee_send_month(request, pk):
    """Mail one employee their own summary for the selected month."""
    org = request.user.organization
    employee = get_object_or_404(Employee, pk=pk, organization=org)
    today = timezone.localdate()
    try:
        year = int(request.POST.get("year", today.year))
        month = int(request.POST.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month

    start, end = month_bounds(year, month)
    sent, failed, skipped, failures = send_monthly_reports(org, start, end, [employee])

    label = f"{calendar.month_name[month]} {year}"
    if failed:
        messages.warning(request, f"Could not email {employee.name}: {failures[0][1]}")
    elif skipped:
        messages.warning(request, f"{employee.name} has no attendance recorded in {label}.")
    else:
        messages.success(request, f"{label} report emailed to {employee.name}.")

    return redirect(f"/employees/{employee.pk}/?year={year}&month={month}")


@org_required
def org_settings(request):
    org = request.user.organization
    today = timezone.localdate()

    # Two independent forms on one page; only the submitted one is bound.
    submitted = request.POST.get("form") if request.method == "POST" else None
    form = OrganizationForm(
        request.POST if submitted == "org" else None, instance=org
    )
    holiday_form = HolidayForm(
        request.POST if submitted == "holiday" else None, organization=org
    )

    if submitted == "org" and form.is_valid():
        form.save()
        messages.success(request, "Organization settings updated.")
        return redirect("org_settings")

    if submitted == "holiday" and holiday_form.is_valid():
        holiday = holiday_form.save()
        messages.success(request, f"{holiday.name} declared a holiday on {holiday.date:%d %b %Y}.")
        return redirect("org_settings")

    return render(
        request,
        "settings.html",
        {
            "form": form,
            "holiday_form": holiday_form,
            "holidays": Holiday.objects.filter(organization=org, date__year=today.year),
            "year": today.year,
        },
    )


@org_required
@require_POST
def holiday_delete(request, pk):
    org = request.user.organization
    holiday = get_object_or_404(Holiday, pk=pk, organization=org)
    name = holiday.name
    holiday.delete()
    messages.success(request, f"Removed the {name} holiday.")
    return redirect("org_settings")


@org_required
def email_log_list(request):
    org = request.user.organization
    status = request.GET.get("status", "")
    logs = EmailLog.objects.filter(organization=org).select_related("employee", "attendance")
    if status in {EmailLog.SENT, EmailLog.FAILED}:
        logs = logs.filter(status=status)
    return render(request, "emails/logs.html", {"logs": logs[:200], "status": status})
