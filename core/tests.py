from datetime import time, timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attendance, Collection, EmailLog, Employee, Organization, User


class SignupTests(TestCase):
    def test_signup_creates_org_and_admin_and_logs_in(self):
        response = self.client.post(
            reverse("signup"),
            {
                "org_name": "Acme Pvt Ltd",
                "org_email": "hr@acme.test",
                "org_phone": "9876543210",
                "full_name": "Riya Sharma",
                "email": "riya@acme.test",
                "password": "sup3rsecret",
                "confirm_password": "sup3rsecret",
            },
        )
        self.assertRedirects(response, reverse("employee_create"))

        org = Organization.objects.get(name="Acme Pvt Ltd")
        self.assertEqual(org.slug, "acme-pvt-ltd")
        user = User.objects.get(email="riya@acme.test")
        self.assertEqual(user.organization, org)
        self.assertTrue(user.check_password("sup3rsecret"))

    def test_mismatched_passwords_are_rejected(self):
        self.client.post(
            reverse("signup"),
            {
                "org_name": "Acme",
                "org_email": "hr@acme.test",
                "full_name": "Riya",
                "email": "riya@acme.test",
                "password": "sup3rsecret",
                "confirm_password": "different1",
            },
        )
        self.assertFalse(Organization.objects.exists())
        self.assertFalse(User.objects.exists())


class TenantTestCase(TestCase):
    """Two organizations, so isolation can be asserted everywhere."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", email="hr@acme.test")
        self.admin = User.objects.create_user(
            email="admin@acme.test", password="pass12345", full_name="Admin", organization=self.org
        )
        self.employee = Employee.objects.create(
            organization=self.org, employee_code="E1", name="Amit Kumar", email="amit@acme.test"
        )

        self.other_org = Organization.objects.create(name="Globex", email="hr@globex.test")
        self.other_employee = Employee.objects.create(
            organization=self.other_org, employee_code="G1", name="Neha", email="neha@globex.test"
        )
        self.client.force_login(self.admin)


class EmployeeTests(TenantTestCase):
    def test_list_shows_only_own_employees(self):
        response = self.client.get(reverse("employee_list"))
        self.assertContains(response, "Amit Kumar")
        self.assertNotContains(response, "Neha")

    def test_cannot_open_another_orgs_employee(self):
        response = self.client.get(reverse("employee_detail", args=[self.other_employee.pk]))
        self.assertEqual(response.status_code, 404)

    def test_duplicate_employee_code_rejected(self):
        response = self.client.post(
            reverse("employee_create"),
            {"employee_code": "E1", "name": "Copy", "email": "copy@acme.test", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(Employee.objects.filter(organization=self.org).count(), 1)

    def test_same_code_allowed_in_different_org(self):
        Employee.objects.create(
            organization=self.other_org, employee_code="E1", name="Other", email="other@globex.test"
        )
        self.assertEqual(Employee.objects.filter(employee_code="E1").count(), 2)


class AttendanceSaveTests(TenantTestCase):
    def post_sheet(self, **overrides):
        payload = {
            "date": timezone.localdate().isoformat(),
            "notify": True,
            "records": [{"employee_id": self.employee.id, "status": "PRESENT", "note": "on time"}],
        }
        payload.update(overrides)
        return self.client.post(
            reverse("attendance_save"), data=payload, content_type="application/json"
        )

    def test_saves_record_and_emails_employee(self):
        response = self.post_sheet()
        data = response.json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["emailed"], 1)
        self.assertEqual(data["failed"], 0)

        record = Attendance.objects.get(employee=self.employee)
        self.assertEqual(record.status, Attendance.PRESENT)
        self.assertEqual(record.marked_by, self.admin)

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["amit@acme.test"])
        self.assertEqual(sent.reply_to, ["hr@acme.test"])
        self.assertIn("Present", sent.subject)
        self.assertIn("Acme", sent.subject)
        self.assertIn("Present — On Time", sent.alternatives[0][0])

        log = EmailLog.objects.get()
        self.assertEqual(log.status, EmailLog.SENT)
        self.assertEqual(log.to_email, "amit@acme.test")

    def test_subject_carries_org_date_employee_and_status(self):
        self.post_sheet(
            records=[
                {"employee_id": self.employee.id, "status": "PRESENT", "check_in_time": "09:05"}
            ]
        )
        today = timezone.localdate()
        self.assertEqual(
            mail.outbox[0].subject,
            f"[Acme] {today:%d %b %Y} — Amit Kumar (E1): Present (in 9:05 AM)",
        )

    def test_body_lists_employee_details(self):
        self.employee.phone = "9876543210"
        self.employee.designation = "Backend Developer"
        self.employee.department = "Engineering"
        self.employee.save()

        self.post_sheet()
        body = mail.outbox[0].alternatives[0][0]
        for detail in [
            "Employee details",
            "Amit Kumar",
            "E1",
            "9876543210",
            "amit@acme.test",
            "Backend Developer",
            "Engineering",
        ]:
            self.assertIn(detail, body)

    def test_half_day_and_absent_emails_use_matching_copy(self):
        self.post_sheet(records=[{"employee_id": self.employee.id, "status": "HALF_DAY"}])
        self.assertIn("Half day", mail.outbox[0].subject)
        self.assertIn("Marked Half Day", mail.outbox[0].alternatives[0][0])

        mail.outbox.clear()
        self.post_sheet(records=[{"employee_id": self.employee.id, "status": "ABSENT"}])
        self.assertIn("Absent", mail.outbox[0].subject)
        self.assertIn("Marked Absent", mail.outbox[0].alternatives[0][0])

    def test_resaving_updates_record_without_duplicating(self):
        self.post_sheet()
        self.post_sheet(records=[{"employee_id": self.employee.id, "status": "ABSENT"}])

        self.assertEqual(Attendance.objects.filter(employee=self.employee).count(), 1)
        self.assertEqual(Attendance.objects.get(employee=self.employee).status, Attendance.ABSENT)
        self.assertEqual(len(mail.outbox), 2)


    def test_notify_false_saves_without_email(self):
        response = self.post_sheet(notify=False)
        self.assertEqual(response.json()["saved"], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailLog.objects.exists())

    def test_future_date_rejected(self):
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.post_sheet(date=future)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Attendance.objects.exists())

    def test_invalid_status_is_ignored(self):
        response = self.post_sheet(
            records=[{"employee_id": self.employee.id, "status": "VACATION"}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Attendance.objects.exists())

    def test_cannot_mark_another_orgs_employee(self):
        response = self.post_sheet(
            records=[{"employee_id": self.other_employee.id, "status": "PRESENT"}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Attendance.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_smtp_failure_still_keeps_attendance(self):
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            with self.assertLogs("core.emails", level="WARNING"):
                from unittest.mock import patch

                with patch(
                    "django.core.mail.EmailMultiAlternatives.send", side_effect=Exception("boom")
                ):
                    response = self.post_sheet()

        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["failures"][0]["name"], "Amit Kumar")
        self.assertTrue(Attendance.objects.filter(employee=self.employee).exists())
        self.assertEqual(EmailLog.objects.get().status, EmailLog.FAILED)


class NoDuplicateMailTests(TenantTestCase):
    """A saved-and-mailed record must not mail again unless something changed."""

    def post_sheet(self, status="PRESENT", check_in=None):
        payload = {
            "date": timezone.localdate().isoformat(),
            "notify": True,
            "records": [
                {"employee_id": self.employee.id, "status": status, "check_in_time": check_in}
            ],
        }
        return self.client.post(
            reverse("attendance_save"), data=payload, content_type="application/json"
        )

    def test_identical_resave_sends_no_mail(self):
        self.post_sheet()
        self.assertEqual(len(mail.outbox), 1)

        response = self.post_sheet()
        data = response.json()

        self.assertEqual(len(mail.outbox), 1, "unchanged status must not re-notify")
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["emailed"], 0)
        self.assertEqual(data["skipped"], 1)
        self.assertIn("unchanged", data["message"])

    def test_status_change_sends_an_update_mail(self):
        self.post_sheet(status="PRESENT")
        mail.outbox.clear()

        response = self.post_sheet(status="ABSENT")
        self.assertEqual(response.json()["emailed"], 1)
        self.assertEqual(response.json()["skipped"], 0)
        self.assertEqual(len(mail.outbox), 1)

        sent = mail.outbox[0]
        self.assertIn("Updated", sent.subject)
        body = sent.alternatives[0][0]
        self.assertIn("changed from <strong>Present</strong> to <strong>Absent</strong>", body)

    def test_first_mail_is_not_labelled_as_an_update(self):
        self.post_sheet()
        self.assertNotIn("Updated", mail.outbox[0].subject)
        self.assertNotIn("Correction", mail.outbox[0].alternatives[0][0])

    def test_lateness_flip_counts_as_a_change(self):
        self.post_sheet(check_in="09:00")
        mail.outbox.clear()

        self.post_sheet(check_in="10:30")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Late by 45 min", mail.outbox[0].subject)

    def test_note_only_edit_does_not_re_notify(self):
        self.post_sheet()
        mail.outbox.clear()

        response = self.client.post(
            reverse("attendance_save"),
            data={
                "date": timezone.localdate().isoformat(),
                "notify": True,
                "records": [
                    {
                        "employee_id": self.employee.id,
                        "status": "PRESENT",
                        "note": "corrected the note only",
                    }
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(
            Attendance.objects.get().note, "corrected the note only", "note must still be saved"
        )

    def test_unchanged_row_retries_when_the_earlier_mail_failed(self):
        from unittest.mock import patch

        with self.assertLogs("core.emails", level="WARNING"):
            with patch(
                "django.core.mail.EmailMultiAlternatives.send", side_effect=Exception("boom")
            ):
                self.post_sheet()

        self.assertEqual(EmailLog.objects.get().status, EmailLog.FAILED)
        self.assertEqual(len(mail.outbox), 0)

        response = self.post_sheet()  # same status, but nothing ever reached them
        self.assertEqual(response.json()["emailed"], 1)
        self.assertEqual(response.json()["skipped"], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_force_resend_ignores_the_change_rule(self):
        self.post_sheet()
        mail.outbox.clear()

        self.client.post(
            reverse("attendance_resend"), {"date": timezone.localdate().isoformat()}
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_notify_false_never_mails_even_on_a_change(self):
        self.post_sheet()
        mail.outbox.clear()

        self.client.post(
            reverse("attendance_save"),
            data={
                "date": timezone.localdate().isoformat(),
                "notify": False,
                "records": [{"employee_id": self.employee.id, "status": "ABSENT"}],
            },
            content_type="application/json",
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Attendance.objects.get().status, Attendance.ABSENT)


class LateArrivalTests(TenantTestCase):
    """Org default: 09:30 start with 15 min grace, so the cutoff is 09:45."""

    def mark(self, status="PRESENT", check_in=None, check_out=None, on=None):
        payload = {
            "date": (on or timezone.localdate()).isoformat(),
            "notify": True,
            "records": [
                {
                    "employee_id": self.employee.id,
                    "status": status,
                    "check_in_time": check_in,
                    "check_out_time": check_out,
                }
            ],
        }
        return self.client.post(
            reverse("attendance_save"), data=payload, content_type="application/json"
        )

    def test_arrival_inside_grace_period_is_on_time(self):
        self.mark(check_in="09:44")
        record = Attendance.objects.get()
        self.assertFalse(record.is_late)
        self.assertEqual(record.minutes_late, 0)
        self.assertIn("Present (in 9:44 AM)", mail.outbox[0].subject)
        self.assertIn("Present — On Time", mail.outbox[0].alternatives[0][0])

    def test_arrival_past_grace_period_is_late(self):
        self.mark(check_in="10:30")
        record = Attendance.objects.get()
        self.assertTrue(record.is_late)
        self.assertEqual(record.minutes_late, 45)
        self.assertEqual(record.late_by_display, "45 min")

        body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Late by 45 min", mail.outbox[0].subject)
        self.assertIn("Present — Late Arrival", body)
        self.assertIn("45 min past the 9:30 AM start time", body)

    def test_late_by_display_formats_hours(self):
        self.mark(check_in="11:50")
        self.assertEqual(Attendance.objects.get().late_by_display, "2 hr 5 min")

    def test_org_timings_change_who_counts_as_late(self):
        self.org.work_start_time = time(11, 0)
        self.org.grace_period_minutes = 0
        self.org.save()

        self.mark(check_in="10:30")
        self.assertFalse(Attendance.objects.get().is_late)

    def test_times_are_discarded_for_absent_and_leave(self):
        self.mark(status="ABSENT", check_in="10:30", check_out="18:00")
        record = Attendance.objects.get()
        self.assertIsNone(record.check_in_time)
        self.assertIsNone(record.check_out_time)
        self.assertFalse(record.is_late)

    def test_worked_hours(self):
        self.mark(check_in="09:30", check_out="18:00")
        self.assertEqual(Attendance.objects.get().worked_hours, 8.5)

    def test_worked_hours_none_when_checkout_missing_or_invalid(self):
        self.mark(check_in="09:30")
        self.assertIsNone(Attendance.objects.get().worked_hours)

        self.mark(check_in="18:00", check_out="09:30")
        self.assertIsNone(Attendance.objects.get().worked_hours)

    def test_invalid_time_string_is_ignored_not_fatal(self):
        response = self.mark(check_in="not-a-time")
        self.assertEqual(response.json()["saved"], 1)
        self.assertIsNone(Attendance.objects.get().check_in_time)

    def test_monthly_email_stats_and_punctuality(self):
        first = timezone.localdate().replace(day=1)
        Attendance.objects.create(
            organization=self.org,
            employee=self.employee,
            date=first,
            status=Attendance.PRESENT,
            check_in_time=time(9, 0),
        )
        Attendance.objects.create(
            organization=self.org,
            employee=self.employee,
            date=first + timedelta(days=1),
            status=Attendance.PRESENT,
            check_in_time=time(10, 30),
        )

        summary = self.employee.month_summary(first)
        self.assertEqual(summary["counts"][Attendance.PRESENT], 2)
        self.assertEqual(summary["timed_days"], 2)
        self.assertEqual(summary["on_time_days"], 1)
        self.assertEqual(summary["late_days"], 1)
        self.assertEqual(summary["punctuality_pct"], 50)
        self.assertEqual(summary["payable_days"], 2.0)

    def test_present_streak_breaks_on_absence(self):
        start = timezone.localdate() - timedelta(days=4)
        statuses = [
            Attendance.PRESENT,
            Attendance.ABSENT,
            Attendance.PRESENT,
            Attendance.PRESENT,
            Attendance.PRESENT,
        ]
        for offset, status in enumerate(statuses):
            Attendance.objects.create(
                organization=self.org,
                employee=self.employee,
                date=start + timedelta(days=offset),
                status=status,
            )
        self.assertEqual(self.employee.present_streak(timezone.localdate()), 3)

    def test_streak_highlight_appears_in_email(self):
        start = timezone.localdate() - timedelta(days=5)
        for offset in range(5):
            Attendance.objects.create(
                organization=self.org,
                employee=self.employee,
                date=start + timedelta(days=offset),
                status=Attendance.PRESENT,
                check_in_time=time(9, 0),
            )
        self.mark(check_in="09:15")
        self.assertIn("days present in a row", mail.outbox[0].alternatives[0][0])

    def test_repeat_late_arrivals_are_called_out(self):
        first = timezone.localdate().replace(day=1)
        Attendance.objects.create(
            organization=self.org,
            employee=self.employee,
            date=first,
            status=Attendance.PRESENT,
            check_in_time=time(10, 30),
        )
        self.mark(check_in="10:30", on=max(first + timedelta(days=1), timezone.localdate()))
        self.assertIn("late arrivals this month", mail.outbox[-1].alternatives[0][0])


class CollectionTests(TenantTestCase):
    """Party-wise orders and collection captured with the day's attendance."""

    def mark(self, lines, status="PRESENT", on=None):
        payload = {
            "date": (on or timezone.localdate()).isoformat(),
            "notify": True,
            "records": [
                {"employee_id": self.employee.id, "status": status, "collections": lines}
            ],
        }
        return self.client.post(
            reverse("attendance_save"), data=payload, content_type="application/json"
        )

    def test_multiple_parties_are_stored_and_totalled(self):
        self.mark(
            [
                {"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"},
                {"party_name": "Party B", "order_amount": "2500", "collected_amount": "2000"},
            ]
        )
        record = Attendance.objects.get()
        self.assertEqual(record.parties_count, 2)
        self.assertEqual(record.orders_count, 2)
        self.assertEqual(record.total_order_amount, Decimal("7500.00"))
        self.assertEqual(record.total_collected, Decimal("7000.00"))

    def test_email_lists_each_party_and_the_total(self):
        self.mark(
            [
                {"party_name": "Sharma Traders", "order_amount": "5000", "collected_amount": "5000"},
                {"party_name": "Verma Stores", "order_amount": "2000", "collected_amount": "2000"},
            ]
        )
        body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Sharma Traders", body)
        self.assertIn("Verma Stores", body)
        self.assertIn("5,000.00", body)
        self.assertIn("2,000.00", body)
        self.assertIn("7,000.00", body)  # collected total
        self.assertRegex(body, r"Orders booked from <strong[^>]*>2</strong>")

    def test_blank_and_zero_lines_are_dropped(self):
        self.mark(
            [
                {"party_name": "Real Party", "order_amount": "100", "collected_amount": "0"},
                {"party_name": "", "order_amount": "500", "collected_amount": "500"},
                {"party_name": "Empty Party", "order_amount": "0", "collected_amount": "0"},
            ]
        )
        record = Attendance.objects.get()
        self.assertEqual(record.parties_count, 1)
        self.assertEqual(record.party_lines[0].party_name, "Real Party")

    def test_negative_and_garbage_amounts_become_zero(self):
        self.mark(
            [
                {"party_name": "Odd Party", "order_amount": "-900", "collected_amount": "abc"},
                {"party_name": "Good Party", "order_amount": "1,500", "collected_amount": "1500"},
            ]
        )
        record = Attendance.objects.get()
        # The first line has no positive amount left, so it is dropped entirely.
        self.assertEqual(record.parties_count, 1)
        self.assertEqual(record.total_order_amount, Decimal("1500.00"))

    def test_resave_replaces_lines_instead_of_appending(self):
        self.mark([{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}])
        self.mark([{"party_name": "Party B", "order_amount": "1000", "collected_amount": "1000"}])

        record = Attendance.objects.get()
        self.assertEqual(record.parties_count, 1)
        self.assertEqual(record.party_lines[0].party_name, "Party B")
        self.assertEqual(Collection.objects.count(), 1)

    def test_changed_collection_triggers_a_fresh_mail(self):
        self.mark([{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}])
        mail.outbox.clear()

        response = self.mark(
            [{"party_name": "Party A", "order_amount": "5000", "collected_amount": "6000"}]
        )
        self.assertEqual(response.json()["emailed"], 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_identical_collection_resave_stays_silent(self):
        lines = [{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}]
        self.mark(lines)
        mail.outbox.clear()

        response = self.mark(lines)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(response.json()["skipped"], 1)

    def test_absent_employee_keeps_no_collection(self):
        self.mark(
            [{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}],
            status="ABSENT",
        )
        self.assertEqual(Collection.objects.count(), 0)

    def test_deleting_attendance_removes_its_lines(self):
        self.mark([{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}])
        Attendance.objects.get().delete()
        self.assertEqual(Collection.objects.count(), 0)

    def test_month_summary_aggregates_business(self):
        first = timezone.localdate().replace(day=1)
        self.mark(
            [{"party_name": "Party A", "order_amount": "5000", "collected_amount": "4000"}],
            on=first,
        )
        if first != timezone.localdate():
            self.mark(
                [{"party_name": "Party B", "order_amount": "3000", "collected_amount": "3000"}]
            )
            expected_orders, expected_order, expected_collected = 2, "8000.00", "7000.00"
        else:
            expected_orders, expected_order, expected_collected = 1, "5000.00", "4000.00"

        summary = self.employee.month_summary(first)
        self.assertEqual(summary["orders_count"], expected_orders)
        self.assertEqual(summary["order_total"], Decimal(expected_order))
        self.assertEqual(summary["collected_total"], Decimal(expected_collected))

    def test_monthly_report_shows_totals(self):
        self.mark(
            [
                {"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"},
                {"party_name": "Party B", "order_amount": "2000", "collected_amount": "2000"},
            ]
        )
        today = timezone.localdate()
        response = self.client.get(
            reverse("report_monthly"), {"year": today.year, "month": today.month}
        )
        row = next(r for r in response.context["rows"] if r["employee"] == self.employee)
        self.assertEqual(row["orders"], 2)
        self.assertEqual(row["order_total"], Decimal("7000.00"))
        self.assertEqual(row["collected"], Decimal("7000.00"))
        self.assertEqual(response.context["totals"]["collected"], Decimal("7000.00"))

    def test_csv_export_includes_business_columns(self):
        self.mark([{"party_name": "Party A", "order_amount": "5000", "collected_amount": "4000"}])
        today = timezone.localdate()
        response = self.client.get(
            reverse("report_monthly"),
            {"year": today.year, "month": today.month, "export": "csv"},
        )
        content = response.content.decode()
        self.assertIn("Order Value", content)
        self.assertIn("Collected", content)
        self.assertIn("5000.00", content)
        self.assertIn("TOTAL", content)

    def test_collection_lines_of_another_org_are_untouched(self):
        other_attendance = Attendance.objects.create(
            organization=self.other_org,
            employee=self.other_employee,
            date=timezone.localdate(),
            status=Attendance.PRESENT,
        )
        Collection.objects.create(
            attendance=other_attendance, party_name="Globex Party", collected_amount=Decimal("100")
        )

        self.mark([{"party_name": "Party A", "order_amount": "5000", "collected_amount": "5000"}])

        response = self.client.get(reverse("report_monthly"))
        names = [row["employee"].name for row in response.context["rows"]]
        self.assertNotIn("Neha", names)
        self.assertEqual(response.context["totals"]["collected"], Decimal("5000.00"))


class RupeeFilterTests(TestCase):
    def test_indian_digit_grouping(self):
        from core.templatetags.money import rupees, rupees_short

        self.assertEqual(rupees(500), "500.00")
        self.assertEqual(rupees(5000), "5,000.00")
        self.assertEqual(rupees(125000), "1,25,000.00")
        self.assertEqual(rupees(Decimal("12345678.5")), "1,23,45,678.50")
        self.assertEqual(rupees("bad"), "0.00")
        self.assertEqual(rupees(None), "0.00")
        self.assertEqual(rupees_short(5000), "5,000")
        self.assertEqual(rupees_short(Decimal("5000.25")), "5,000.25")


class OrgSettingsTests(TenantTestCase):
    def test_admin_can_change_office_timings(self):
        response = self.client.post(
            reverse("org_settings"),
            {
                "name": "Acme",
                "email": "hr@acme.test",
                "phone": "",
                "address": "",
                "work_start_time": "10:00",
                "grace_period_minutes": 5,
            },
        )
        self.assertRedirects(response, reverse("org_settings"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.work_start_time, time(10, 0))
        self.assertEqual(self.org.grace_period_minutes, 5)


class SheetAndReportTests(TenantTestCase):
    def test_sheet_lists_only_active_employees_of_org(self):
        Employee.objects.create(
            organization=self.org,
            employee_code="E2",
            name="Inactive Guy",
            email="ig@acme.test",
            is_active=False,
        )
        response = self.client.get(reverse("attendance_sheet"))
        self.assertContains(response, "Amit Kumar")
        self.assertNotContains(response, "Inactive Guy")
        self.assertNotContains(response, "Neha")

    def test_monthly_report_counts_payable_days(self):
        today = timezone.localdate().replace(day=1)
        Attendance.objects.create(
            organization=self.org, employee=self.employee, date=today, status=Attendance.PRESENT
        )
        Attendance.objects.create(
            organization=self.org,
            employee=self.employee,
            date=today + timedelta(days=1),
            status=Attendance.HALF_DAY,
        )
        response = self.client.get(
            reverse("report_monthly"), {"year": today.year, "month": today.month}
        )
        row = next(r for r in response.context["rows"] if r["employee"] == self.employee)
        self.assertEqual(row["present"], 1)
        self.assertEqual(row["half_day"], 1)
        self.assertEqual(row["payable"], 1.5)

    def test_csv_export(self):
        today = timezone.localdate()
        Attendance.objects.create(
            organization=self.org, employee=self.employee, date=today, status=Attendance.PRESENT
        )
        response = self.client.get(
            reverse("report_monthly"),
            {"year": today.year, "month": today.month, "export": "csv"},
        )
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("Amit Kumar", response.content.decode())

    def test_resend_mails_for_a_date(self):
        today = timezone.localdate()
        Attendance.objects.create(
            organization=self.org, employee=self.employee, date=today, status=Attendance.PRESENT
        )
        self.client.post(reverse("attendance_resend"), {"date": today.isoformat()})
        self.assertEqual(len(mail.outbox), 1)


class AuthGuardTests(TestCase):
    def test_protected_pages_redirect_anonymous_users(self):
        for name in ["dashboard", "employee_list", "attendance_sheet", "report_monthly"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response.url)
