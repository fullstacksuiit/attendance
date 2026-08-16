# Attendance Manager

Multi-tenant employee attendance system. An organization signs up, adds its
employees, and marks a daily attendance sheet — every employee then receives an
email telling them whether they were marked **Present**, **Absent**, **Half Day**
or **Leave**.

- **Backend:** Django 4.2 (SQLite by default)
- **Frontend:** Django templates + plain HTML/CSS/JS (no build step, no framework)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ and click **Create an account**.

### Email (Gmail)

Settings live directly in `attendance/settings.py`:

```python
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = "your-email@gmail.com"
EMAIL_HOST_PASSWORD = "abcdefghijklmnop"   # Gmail App Password, spaces removed
DEFAULT_FROM_EMAIL = "Maktab <your-email@gmail.com>"
```

Gmail rejects normal passwords — create an **App Password** (Google Account →
Security → 2-Step Verification → App passwords). Google displays it as
`abcd efgh ijkl mnop`; paste it **without the spaces**.

To print mails to the terminal instead of sending them while developing, swap
one line:

```python
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
```

Tests never send real mail — Django swaps in an in-memory backend automatically.

### Email troubleshooting

`530 Authentication Required` means Gmail got an empty or wrong password — check
the App Password is pasted with its spaces removed and that 2-Step Verification
is on for that account.

`CERTIFICATE_VERIFY_FAILED` on macOS means the python.org build has no CA bundle.
`certifi` is in `requirements.txt` and `settings.py` points OpenSSL at it
automatically when the platform has none; the alternative is running
`/Applications/Python 3.x/Install Certificates.command` once.

To check credentials without sending anything:

```bash
python -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','attendance.settings');django.setup();from django.core.mail import get_connection;c=get_connection();c.open();print('SMTP OK');c.close()"
```

Restart the server after editing settings, and keep only **one** `runserver`
running — a stale process serves stale code and the symptom looks like a bug.

## How it works

| Step | Where |
| --- | --- |
| Org signs up (creates `Organization` + first admin `User`) | `/signup/` |
| Admin adds employees (name, code, email, department…) | `/employees/add/` |
| Admin picks a date and marks everyone on one sheet | `/attendance/` |
| **Save & Notify** writes the records and emails each employee | `POST /attendance/save/` |
| Monthly summary with payable days + CSV export | `/reports/monthly/` |
| Email each employee their own month-end statement | `POST /reports/monthly/send/` |
| Delivery status of every mail sent | `/emails/logs/` |
| Office timings, grace period, declared holidays | `/settings/` |

### Working days, weekly offs and holidays

Each employee has their own weekly schedule — ticked weekdays on the employee
form, defaulting to **Mon–Sat** with Sunday off. The owner declares one-off
holidays (Diwali, Independence Day…) at `/settings/`, and those apply to
everyone.

A month's working days are derived, never typed:

```
working days = scheduled weekdays in the month − declared holidays on those weekdays
```

August 2026 for a Mon–Sat employee: 31 days − 5 Sundays = 26, minus one declared
holiday = **25**. A Mon–Fri colleague gets 20 for the same month. A holiday that
lands on someone's weekly off is not double-counted.

On the attendance sheet, a weekly-off or holiday row is badged and marked
`Week Off` / `Holiday` automatically — **no mail goes out for those days**.
Picking a status overrides it, so Sunday duty is one click. The "Mark all"
buttons deliberately skip those rows so a bulk Present never sweeps up a Sunday.

`Week Off` and `Holiday` never count as absence, never break a present streak,
and carry no payable weight. Attendance % is measured as
`payable days ÷ working days`, so a lightly-marked month cannot inflate it.

### Check-in time and late arrivals

Each row on the sheet takes an optional **check-in** and **check-out** time.
An arrival is late when it lands past the organization's `work_start_time` plus
`grace_period_minutes` (default 09:30 + 15 min), both editable at `/settings/`.
The sheet flags late rows live as you type; the same rule is recomputed
server-side in `Attendance.minutes_late`, so the email never disagrees with the UI.

Times are only stored for **Present** and **Half Day** — marking someone Absent
discards any time that was typed.

`check_out_time` gives `worked_hours`, shown on the employee page and in the mail.

### Orders and collection (party-wise)

Each employee's row on the sheet opens a panel where the admin adds **one line
per party** — party name, order amount, collected amount. Any number of parties
per employee per day; totals are computed, never typed.

```
Sharma Traders    ₹5,000.00    ₹5,000.00
Verma Stores      ₹2,500.00    ₹2,000.00
Gupta Agency      ₹1,200.00    ₹    0.00
Total · 3 parties ₹8,700.00    ₹7,000.00
```

The employee's mail carries exactly this table plus the grand total, and
"Orders booked from 3 customers today." **Orders** counts parties with a
non-zero order amount, so a pure collection visit is not miscounted as a sale.

Rules worth knowing:

- A line needs a party name **and** a non-zero amount, otherwise it is dropped —
  blank rows left in the editor cost nothing.
- Negative or non-numeric amounts become 0.
- Saving **replaces** that day's lines rather than appending, so editing a party
  or deleting one behaves the way it looks.
- An employee marked Absent or on Leave stores no lines.
- Changing any figure counts as a change for the mail rule below, so a corrected
  collection re-notifies the employee.

Totals roll up to the dashboard (today + this month), the monthly report (per
employee plus a TOTAL row), the CSV export, and each employee's own page.
Amounts render with Indian grouping (`1,25,000.00`) via the `rupees` filter in
[core/templatetags/money.py](core/templatetags/money.py).

### Monthly statement by email

**Email report to staff** on the monthly report page mails every active employee
their *own* month-end statement — never the whole team's table to everyone.
The same button per person sits on their detail page.

Each statement carries working days, payable days, attendance %, the status
counts, punctuality, the month's orders and collection, and a day-by-day table
with check-in times, late minutes, notes and daily collection.

- Employees with **no attendance recorded** that month are skipped, not sent an
  empty statement; the count is reported back.
- Inactive employees are excluded.
- A month that has not started yet is refused.
- Sending mid-month is fine — the mail says "August 2026 so far, as on 17 Aug".
- Every send is written to the email log like any other mail, with no attendance
  row attached since a statement belongs to no single day.

### One mail per change, never a duplicate

Re-saving a sheet does **not** re-mail everyone. On save, each row is compared
against what was already stored and a mail goes out only when:

- the record is new, **or**
- the status changed (Present → Absent), **or**
- the late flag flipped (an edited check-in time crossed the cutoff), **or**
- the party-wise orders/collection figures changed, **or**
- no mail for that record was ever successfully delivered — a row whose earlier
  send failed is retried rather than silently skipped

Editing only a note leaves the note saved and sends nothing. The response
reports it: *"Saved 12 record(s), emailed 2. 10 unchanged — no mail re-sent."*

A changed record is mailed as a **correction**: the subject gains an `Updated ·`
prefix and the body opens with "changed from Absent to Present. This replaces
the earlier mail."

**Force re-send** on the attendance page deliberately ignores this rule and
mails everyone marked on that date — that is the escape hatch for a batch that
failed to deliver.

### What the email says

The subject leads with the org, then the date, then who and what:

```
[Acme Pvt Ltd] 17 Aug 2026 — Amit Kumar (ACM-014): Present (in 9:20 AM)
[Acme Pvt Ltd] 17 Aug 2026 — Amit Kumar (ACM-014): Late by 45 min
[Acme Pvt Ltd] Updated · 17 Aug 2026 — Amit Kumar (ACM-014): Absent
```

The body carries the full record: today's status with check-in/check-out and
hours worked, the **party-wise orders and collection** table with its total, an
**Employee details** block (name, employee ID, phone, email, designation,
department, date of joining), and the employee's **month so far** — present /
half day / absent / payable days, punctuality ("on time on 8 of 10 days"), and
the month's orders and collection. A highlight line leads with the day's
collection when there is one, otherwise a present streak or repeated lateness.

Punctual days get recognition; late arrivals get the exact minutes and the
month's late count. The motivation is the employee's own numbers, not adjectives.

Copy for each status lives in `build_copy()` and `build_highlight()` in
[core/emails.py](core/emails.py) — that is the one place to edit the tone.

### Attendance sheet

One row per active employee, one radio button per status, plus an optional note
that is included in the mail. "Mark all" buttons fill the whole column in one
click. Saving is a `fetch()` POST — the page reports how many records were saved
and how many mails went out without a reload.

Re-saving the same date **updates** the existing records (there is a unique
constraint on employee + date) and re-sends those mails.

### Email delivery is non-blocking for data

Mails are sent after the database transaction commits, over a single reused SMTP
connection. If SMTP fails, the attendance stays saved, the failure is written to
`EmailLog`, and the failing employee names come back in the response. A
**Re-send mails for this date** button retries them.

## Multi-tenancy

Every `Employee`, `Attendance` and `EmailLog` row carries an `organization` FK,
and every view filters on `request.user.organization`. Employee codes and emails
are unique *per organization*, so two companies can both have an "E1". Requesting
another org's employee returns 404. This is covered by tests.

## Data model

```
Organization ─┬─ User (admin, logs in with email)
              ├─ Employee ──┬─ Attendance (employee + date unique)
              │  (working_  │      └─ Collection (party_name, order, collected)
              │   days)     └─ EmailLog (SENT / FAILED + error)
              └─ Holiday (org + date unique)
```

`Attendance.STATUS_WEIGHTS` drives the payable-days column in reports:
Present = 1.0, Half Day = 0.5, everything else = 0.
`Employee.working_days` stores the schedule as weekday digits — `"012345"` is
Mon–Sat, where 0 = Monday, matching `date.weekday()`.

## Tests

```bash
python manage.py test core
```

88 tests covering signup, tenant isolation, monthly statements by email
(per-employee content, skipping employees with no attendance, inactive and
foreign-org exclusion, failure logging, future months, login guard),
per-employee working days (Mon–Sat
and Mon–Fri schedules, declared holidays, holidays landing on a weekly off,
week-off rows saved but never mailed, overrides, streaks), holiday admin and its
tenant guard, saving the sheet, the email subject
and body per status, the no-duplicate-mail rule (identical resave stays silent,
status/lateness/collection changes re-notify, note-only edits do not, failed
sends are retried, force re-send overrides), late-arrival maths (grace period,
hour formatting, org timing changes, times discarded for absentees), worked
hours, party-wise collection (multi-party totals, blank and negative amounts,
replace-not-append on resave, cascade delete, report and CSV roll-ups), rupee
grouping, monthly stats and streaks, duplicate/future-date/foreign-employee
rejection, SMTP failure handling, and login guards.

## Django admin

```bash
python manage.py createsuperuser
```

Then http://127.0.0.1:8000/django-admin/ for raw access to all models.

## Before deploying

- Move `EMAIL_HOST_PASSWORD` out of `settings.py` into an environment variable —
  it is a live credential sitting in a tracked file
- Set `DEBUG=False`, a fresh `SECRET_KEY`, and a real `ALLOWED_HOSTS` in `.env`
- Switch `DATABASES` to Postgres
- Run `python manage.py collectstatic` and serve `staticfiles/` from your web server
- If you grow past a few hundred employees per sheet, move
  `send_attendance_emails()` into a background worker (Celery/RQ) — right now the
  admin's save request waits for SMTP.
