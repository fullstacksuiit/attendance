import hashlib
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Organization(models.Model):
    """A tenant. Every employee and attendance row belongs to exactly one."""

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True, blank=True)
    email = models.EmailField(help_text="Contact email shown on outgoing attendance mails.")
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    work_start_time = models.TimeField(
        default=time(9, 30), help_text="Office start time used to flag late arrivals."
    )
    grace_period_minutes = models.PositiveIntegerField(
        default=15, help_text="Minutes after start time before an arrival counts as late."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "org"
            slug = base
            counter = 2
            while Organization.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        return super().save(*args, **kwargs)


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Org admin. Employees are not users — they only receive email."""

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="admins",
        null=True,
        blank=True,
        help_text="Null for platform superusers.",
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    def __str__(self):
        return self.email

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return (self.full_name or self.email).split(" ")[0]


class Holiday(models.Model):
    """A day the owner has declared off for the whole organization."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="holidays"
    )
    date = models.DateField()
    name = models.CharField(max_length=120, help_text="e.g. Diwali, Independence Day")

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "date"], name="unique_holiday_per_org_per_day"
            )
        ]

    def __str__(self):
        return f"{self.name} — {self.date}"


#: Monday = 0 … Sunday = 6, matching ``date.weekday()``.
WEEKDAYS = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]

#: Mon–Sat, the common Indian six-day week.
DEFAULT_WORKING_DAYS = "012345"


class Employee(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="employees"
    )
    employee_code = models.CharField(max_length=40)
    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    department = models.CharField(max_length=100, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    working_days = models.CharField(
        max_length=7,
        default=DEFAULT_WORKING_DAYS,
        help_text="Weekday numbers this employee works — 0=Monday … 6=Sunday.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "employee_code"], name="unique_employee_code_per_org"
            ),
            models.UniqueConstraint(
                fields=["organization", "email"], name="unique_employee_email_per_org"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.employee_code})"

    @property
    def initials(self):
        parts = [part for part in self.name.split() if part]
        return "".join(part[0] for part in parts[:2]).upper() or "?"

    @property
    def working_weekdays(self):
        return {int(char) for char in self.working_days if char.isdigit()}

    @property
    def working_days_display(self):
        """"Mon–Sat" when the week is contiguous, otherwise "Mon, Wed, Fri"."""
        days = sorted(self.working_weekdays)
        if not days:
            return "None"
        labels = [dict(WEEKDAYS)[day][:3] for day in days]
        is_run = days == list(range(days[0], days[-1] + 1))
        return f"{labels[0]}–{labels[-1]}" if is_run and len(days) > 2 else ", ".join(labels)

    @property
    def weekly_off_display(self):
        off = sorted(set(range(7)) - self.working_weekdays)
        return ", ".join(dict(WEEKDAYS)[day][:3] for day in off) or "None"

    def works_on(self, day):
        return day.weekday() in self.working_weekdays

    def working_days_between(self, start, end, holiday_dates=()):
        """Scheduled days in ``[start, end]``, excluding declared holidays.

        ``holiday_dates`` is passed in rather than queried so a report can load
        the org's holidays once for every employee.
        """
        weekdays = self.working_weekdays
        holidays = set(holiday_dates)
        total = 0
        day = start
        while day <= end:
            if day.weekday() in weekdays and day not in holidays:
                total += 1
            day += timedelta(days=1)
        return total

    def month_summary(self, on_date, holiday_dates=None):
        """Counts, payable days and punctuality for the month containing `on_date`.

        Used to give the employee their standing in the notification email.
        """
        start = on_date.replace(day=1)
        if start.month == 12:
            end = date_cls(start.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date_cls(start.year, start.month + 1, 1) - timedelta(days=1)

        if holiday_dates is None:
            holiday_dates = Holiday.objects.filter(
                organization_id=self.organization_id, date__range=(start, end)
            ).values_list("date", flat=True)
        working_days = self.working_days_between(start, end, holiday_dates)

        records = list(self.attendance_records.filter(date__range=(start, end)))
        counts = {status: 0 for status, _ in Attendance.STATUS_CHOICES}
        for record in records:
            counts[record.status] += 1

        # Week offs and declared holidays are neither worked nor owed.
        records = [r for r in records if r.status not in Attendance.NON_WORKING_STATUSES]

        attended = [r for r in records if r.status in (Attendance.PRESENT, Attendance.HALF_DAY)]
        timed = [r for r in attended if r.check_in_time]
        on_time = [r for r in timed if not r.is_late]

        business = Collection.objects.filter(
            attendance__employee=self, attendance__date__range=(start, end)
        ).aggregate(
            orders=models.Count("id", filter=models.Q(order_amount__gt=0)),
            parties=models.Count("id"),
            order_total=models.Sum("order_amount"),
            collected_total=models.Sum("collected_amount"),
        )

        return {
            "orders_count": business["orders"] or 0,
            "parties_count": business["parties"] or 0,
            "order_total": business["order_total"] or Decimal("0"),
            "collected_total": business["collected_total"] or Decimal("0"),
            "counts": counts,
            "marked_days": len(records),
            "working_days": working_days,
            "payable_days": round(sum(r.day_value for r in records), 1),
            # Measured against the days they were scheduled to work, so a light
            # month of marking cannot inflate the percentage.
            "attendance_pct": round(
                min(sum(r.day_value for r in records) / working_days, 1) * 100
            ) if working_days else 0,
            "on_time_days": len(on_time),
            "timed_days": len(timed),
            "punctuality_pct": round(len(on_time) / len(timed) * 100) if timed else None,
            "late_days": len(timed) - len(on_time),
        }

    def present_streak(self, upto):
        """Consecutive working days ending at `upto` where the employee showed up.

        A week off or declared holiday is skipped rather than counted or treated
        as a break — a Sunday should not cost anyone their streak.
        """
        streak = 0
        records = self.attendance_records.filter(date__lte=upto).order_by("-date")
        for record in records:
            if record.status in Attendance.NON_WORKING_STATUSES:
                continue
            if record.status == Attendance.PRESENT:
                streak += 1
            else:
                break
        return streak


class Attendance(models.Model):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    HALF_DAY = "HALF_DAY"
    LEAVE = "LEAVE"
    WEEK_OFF = "WEEK_OFF"
    HOLIDAY = "HOLIDAY"

    STATUS_CHOICES = [
        (PRESENT, "Present"),
        (ABSENT, "Absent"),
        (HALF_DAY, "Half Day"),
        (LEAVE, "Leave"),
        (WEEK_OFF, "Week Off"),
        (HOLIDAY, "Holiday"),
    ]

    #: The four an admin picks by hand; the other two are filled in by schedule.
    MARKABLE_STATUSES = [PRESENT, ABSENT, HALF_DAY, LEAVE]

    #: Days off nobody needs to be told about, and that never count as absence.
    NON_WORKING_STATUSES = {WEEK_OFF, HOLIDAY}

    #: Weight used when computing payable days in reports.
    STATUS_WEIGHTS = {
        PRESENT: 1.0,
        HALF_DAY: 0.5,
        ABSENT: 0.0,
        LEAVE: 0.0,
        WEEK_OFF: 0.0,
        HOLIDAY: 0.0,
    }

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="attendance_records"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="attendance_records"
    )
    date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_time = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    marked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="marked_attendance"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "employee__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "date"], name="unique_attendance_per_employee_per_day"
            )
        ]
        indexes = [models.Index(fields=["organization", "date"])]

    def __str__(self):
        return f"{self.employee.name} — {self.date} — {self.get_status_display()}"

    @property
    def day_value(self):
        return self.STATUS_WEIGHTS.get(self.status, 0.0)

    @property
    def minutes_late(self):
        """Minutes past the org's start time + grace period. 0 when on time."""
        if not self.check_in_time or self.status not in (self.PRESENT, self.HALF_DAY):
            return 0
        org = self.organization
        deadline = (
            datetime.combine(self.date, org.work_start_time)
            + timedelta(minutes=org.grace_period_minutes)
        )
        arrival = datetime.combine(self.date, self.check_in_time)
        return max(int((arrival - deadline).total_seconds() // 60), 0)

    @property
    def is_late(self):
        return self.minutes_late > 0

    @property
    def late_by_display(self):
        minutes = self.minutes_late
        if not minutes:
            return ""
        hours, mins = divmod(minutes, 60)
        if hours and mins:
            return f"{hours} hr {mins} min"
        if hours:
            return f"{hours} hr"
        return f"{mins} min"

    # Totals read from `self.collections.all()` so a prefetch is reused rather
    # than firing one aggregate query per record.

    @property
    def party_lines(self):
        return list(self.collections.all())

    @property
    def parties_count(self):
        return len(self.party_lines)

    @property
    def orders_count(self):
        """Customers who actually placed an order today."""
        return sum(1 for line in self.party_lines if line.order_amount > 0)

    @property
    def total_order_amount(self):
        return sum((line.order_amount for line in self.party_lines), Decimal("0"))

    @property
    def total_collected(self):
        return sum((line.collected_amount for line in self.party_lines), Decimal("0"))

    @property
    def has_business(self):
        return bool(self.party_lines)

    @property
    def content_signature(self):
        """Everything about this day that shows up in the employee's mail.

        Two records with the same signature would produce identical mails, so
        this is what decides whether another notification is worth sending.
        """
        lines = sorted(
            (line.party_name, str(line.order_amount), str(line.collected_amount))
            for line in self.party_lines
        )
        return "|".join(
            [
                self.status,
                self.check_in_time.isoformat() if self.check_in_time else "",
                self.check_out_time.isoformat() if self.check_out_time else "",
                self.note,
                ";".join(",".join(line) for line in lines),
            ]
        )

    @property
    def content_hash(self):
        return hashlib.sha256(self.content_signature.encode()).hexdigest()[:32]

    @property
    def worked_hours(self):
        """Hours between check-in and check-out, or None if either is missing."""
        if not self.check_in_time or not self.check_out_time:
            return None
        start = datetime.combine(self.date, self.check_in_time)
        end = datetime.combine(self.date, self.check_out_time)
        if end <= start:
            return None
        return round((end - start).total_seconds() / 3600, 1)


class Collection(models.Model):
    """One party's business for one employee on one day.

    A day can have any number of these — the party-wise lines the employee sees
    in their mail, and the totals the reports add up.
    """

    attendance = models.ForeignKey(
        Attendance, on_delete=models.CASCADE, related_name="collections"
    )
    party_name = models.CharField(max_length=150)
    order_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Value of the order booked from this party.",
    )
    collected_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Payment actually collected from this party.",
    )

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["attendance"])]

    def __str__(self):
        return f"{self.party_name} — order {self.order_amount}, collected {self.collected_amount}"


class EmailLog(models.Model):
    SENT = "SENT"
    FAILED = "FAILED"
    STATUS_CHOICES = [(SENT, "Sent"), (FAILED, "Failed")]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="email_logs"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="email_logs"
    )
    attendance = models.ForeignKey(
        Attendance, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_logs"
    )
    to_email = models.EmailField()
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error = models.TextField(blank=True)
    content_hash = models.CharField(
        max_length=32,
        blank=True,
        help_text="Attendance.content_hash at send time — lets a re-save skip "
        "employees who already hold a mail with these exact details.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.to_email} — {self.status}"
