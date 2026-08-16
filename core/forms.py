from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction

from .models import DEFAULT_WORKING_DAYS, WEEKDAYS, Employee, Holiday, Organization, User


class OrganizationSignupForm(forms.Form):
    """Creates the Organization and its first admin user in one step."""

    org_name = forms.CharField(label="Organization name", max_length=150)
    org_email = forms.EmailField(label="Organization contact email")
    org_phone = forms.CharField(label="Phone", max_length=20, required=False)

    full_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Your login email")
    password = forms.CharField(label="Password", widget=forms.PasswordInput, min_length=8)
    confirm_password = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email is already registered. Try logging in.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("confirm_password")
        if password and confirm and password != confirm:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned

    @transaction.atomic
    def save(self):
        data = self.cleaned_data
        organization = Organization.objects.create(
            name=data["org_name"],
            email=data["org_email"],
            phone=data.get("org_phone", ""),
        )
        user = User.objects.create_user(
            email=data["email"],
            password=data["password"],
            full_name=data["full_name"],
            organization=organization,
        )
        return user


class EmailLoginForm(AuthenticationForm):
    username = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"autofocus": True}))


class OrganizationForm(forms.ModelForm):
    """Org profile + the timings that decide who counts as late."""

    class Meta:
        model = Organization
        fields = ["name", "email", "phone", "address", "work_start_time", "grace_period_minutes"]
        widgets = {
            "work_start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "address": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "email": "Contact email (employees reply here)",
            "work_start_time": "Office start time",
            "grace_period_minutes": "Grace period (minutes)",
        }


class HolidayForm(forms.ModelForm):
    """A day the owner declares off for everyone."""

    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

    def clean_date(self):
        day = self.cleaned_data["date"]
        if Holiday.objects.filter(organization=self.organization, date=day).exists():
            raise forms.ValidationError("A holiday is already declared for this date.")
        return day

    def save(self, commit=True):
        holiday = super().save(commit=False)
        holiday.organization = self.organization
        if commit:
            holiday.save()
        return holiday


class EmployeeForm(forms.ModelForm):
    working_days = forms.MultipleChoiceField(
        choices=WEEKDAYS,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Working days",
        help_text="Unticked days are weekly offs — no attendance mail goes out on them.",
    )

    class Meta:
        model = Employee
        fields = [
            "employee_code",
            "name",
            "email",
            "phone",
            "designation",
            "department",
            "date_of_joining",
            "working_days",
            "is_active",
        ]
        widgets = {"date_of_joining": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        # The model stores "012345"; the form works in a list of weekday numbers.
        initial = self.instance.working_days if self.instance.pk else DEFAULT_WORKING_DAYS
        # ModelForm already seeded self.initial with the raw "012345" string, and that
        # wins over the field's own initial — so overwrite it with the list of days.
        self.initial["working_days"] = list(initial)

    def clean_working_days(self):
        days = self.cleaned_data.get("working_days") or []
        if not days:
            raise forms.ValidationError("Pick at least one working day.")
        return "".join(sorted(days))

    def _duplicate_exists(self, **lookup):
        qs = Employee.objects.filter(organization=self.organization, **lookup)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        return qs.exists()

    def clean_employee_code(self):
        code = self.cleaned_data["employee_code"].strip()
        if self._duplicate_exists(employee_code__iexact=code):
            raise forms.ValidationError("An employee with this code already exists.")
        return code

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if self._duplicate_exists(email__iexact=email):
            raise forms.ValidationError("An employee with this email already exists.")
        return email

    def save(self, commit=True):
        employee = super().save(commit=False)
        employee.organization = self.organization
        if commit:
            employee.save()
        return employee
