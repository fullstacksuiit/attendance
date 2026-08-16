from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import Attendance, Collection, EmailLog, Employee, Organization, User


class UserCreateForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "full_name", "organization")


class UserEditForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreateForm
    form = UserEditForm
    model = User
    list_display = ("email", "full_name", "organization", "is_staff", "is_active")
    list_filter = ("is_staff", "is_active", "organization")
    search_fields = ("email", "full_name")
    ordering = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "organization")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "organization", "password1", "password2"),
            },
        ),
    )


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "created_at")
    search_fields = ("name", "email")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("name", "employee_code", "email", "organization", "designation", "is_active")
    list_filter = ("organization", "is_active", "department")
    search_fields = ("name", "email", "employee_code")


class CollectionInline(admin.TabularInline):
    model = Collection
    extra = 0


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "date", "status", "check_in_time",
        "total_order_amount", "total_collected", "organization",
    )
    list_filter = ("status", "organization", "date")
    search_fields = ("employee__name", "employee__email")
    date_hierarchy = "date"
    inlines = [CollectionInline]


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ("party_name", "attendance", "order_amount", "collected_amount")
    search_fields = ("party_name", "attendance__employee__name")


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("to_email", "employee", "status", "subject", "created_at")
    list_filter = ("status", "organization")
    search_fields = ("to_email", "subject")
