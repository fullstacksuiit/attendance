from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .forms import EmailLoginForm

urlpatterns = [
    # auth
    path("", views.dashboard, name="dashboard"),
    path("signup/", views.signup_view, name="signup"),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            authentication_form=EmailLoginForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # employees
    path("employees/", views.employee_list, name="employee_list"),
    path("employees/add/", views.employee_create, name="employee_create"),
    path("employees/<int:pk>/", views.employee_detail, name="employee_detail"),
    path("employees/<int:pk>/edit/", views.employee_edit, name="employee_edit"),
    path("employees/<int:pk>/delete/", views.employee_delete, name="employee_delete"),
    path("employees/<int:pk>/toggle/", views.employee_toggle_active, name="employee_toggle"),
    path("employees/<int:pk>/send-month/", views.employee_send_month, name="employee_send_month"),
    # attendance
    path("attendance/", views.attendance_sheet, name="attendance_sheet"),
    path("attendance/save/", views.attendance_save, name="attendance_save"),
    path("attendance/resend/", views.attendance_resend, name="attendance_resend"),
    # reports
    path("reports/monthly/", views.report_monthly, name="report_monthly"),
    path("reports/monthly/send/", views.report_send, name="report_send"),
    path("emails/logs/", views.email_log_list, name="email_logs"),
    path("settings/", views.org_settings, name="org_settings"),
    path("settings/holidays/<int:pk>/delete/", views.holiday_delete, name="holiday_delete"),
]
