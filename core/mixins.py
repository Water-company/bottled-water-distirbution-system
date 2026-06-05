from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import redirect

from accounts.models import UserRole
from core.navigation import get_user_home_url


class CustomerRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and getattr(user, "is_customer", False)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.warning(self.request, "That page is only available to customer accounts.")
            return redirect(get_user_home_url(self.request.user))
        return super().handle_no_permission()


class RoleRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    allowed_roles = ()
    denied_message = "You do not have access to that page."

    def test_func(self):
        return self.request.user.role in self.allowed_roles

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.warning(self.request, self.denied_message)
            return redirect(get_user_home_url(self.request.user))
        return super().handle_no_permission()


class AgentManagerRequiredMixin(RoleRequiredMixin):
    allowed_roles = (UserRole.AGENT_MANAGER,)
    denied_message = "That page is only available to agent managers."


class DriverRequiredMixin(RoleRequiredMixin):
    allowed_roles = (UserRole.DRIVER,)
    denied_message = "That page is only available to drivers."


class CompanyAdminRequiredMixin(RoleRequiredMixin):
    allowed_roles = (UserRole.COMPANY_ADMIN,)
    denied_message = "That page is only available to company admins."


class SystemAdminRequiredMixin(RoleRequiredMixin):
    allowed_roles = (UserRole.SYSTEM_ADMIN,)
    denied_message = "That page is only available to system admins."
