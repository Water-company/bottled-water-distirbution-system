from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accounts.models import RegistrationOTP, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "role", "phone_number", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name", "phone_number")
    readonly_fields = ("last_login", "date_joined", "created_at", "updated_at", "email_verified_at")
    list_filter = ("role", "is_active", "is_staff", "is_customer")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "phone_number", "profile_image", "role")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "is_customer", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined", "created_at", "updated_at", "email_verified_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "first_name", "last_name", "phone_number", "role", "password1", "password2"),
            },
        ),
    )


@admin.register(RegistrationOTP)
class RegistrationOTPAdmin(admin.ModelAdmin):
    list_display = ("email", "code", "expires_at", "consumed_at", "created_at")
    search_fields = ("email", "user__email", "code")
