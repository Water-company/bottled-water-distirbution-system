from django.urls import reverse

from accounts.models import UserRole


def get_user_home_url(user):
    if not getattr(user, "is_authenticated", False):
        return reverse("home")
    if getattr(user, "role", UserRole.CUSTOMER) == UserRole.CUSTOMER:
        return reverse("accounts:dashboard")
    if getattr(user, "role", "") == UserRole.AGENT_MANAGER:
        return reverse("accounts:agent_dashboard")
    if getattr(user, "role", "") == UserRole.DRIVER:
        return reverse("accounts:driver_dashboard")
    if getattr(user, "role", "") == UserRole.COMPANY_ADMIN:
        return reverse("accounts:company_dashboard")
    if getattr(user, "role", "") == UserRole.SYSTEM_ADMIN:
        return reverse("accounts:system_dashboard")
    return reverse("home")
