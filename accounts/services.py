import logging
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from accounts.models import RegistrationOTP, User, UserRole
from core.services import notify_user


logger = logging.getLogger(__name__)


def get_company_admin_users(company):
    queryset = User.objects.filter(
        role=UserRole.COMPANY_ADMIN,
        managed_company=company,
    ).order_by("first_name", "last_name", "email")
    primary_admin = getattr(company, "admin", None)
    if primary_admin and primary_admin.role == UserRole.COMPANY_ADMIN and primary_admin.managed_company_id == company.pk:
        queryset = queryset | User.objects.filter(pk=primary_admin.pk)
    return queryset.distinct()


def _generate_numeric_otp(length=6):
    upper_bound = 10 ** length
    return f"{secrets.randbelow(upper_bound):0{length}d}"


def get_registration_resend_wait_seconds(user):
    latest_otp = user.registration_otps.order_by("-created_at").first()
    if latest_otp is None:
        return 0

    cooldown_window = timezone.timedelta(seconds=settings.REGISTRATION_OTP_RESEND_SECONDS)
    available_at = latest_otp.created_at + cooldown_window
    remaining_seconds = int((available_at - timezone.now()).total_seconds())
    return max(0, remaining_seconds)


def create_registration_otp(user):
    resend_wait_seconds = get_registration_resend_wait_seconds(user)
    if resend_wait_seconds:
        raise ValidationError(
            f"Please wait {resend_wait_seconds} second{'s' if resend_wait_seconds != 1 else ''} before requesting another OTP."
        )

    now = timezone.now()
    with transaction.atomic():
        RegistrationOTP.objects.filter(
            user=user,
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).update(consumed_at=now)
        otp = RegistrationOTP.objects.create(
            user=user,
            email=user.email,
            code=_generate_numeric_otp(),
            expires_at=now + timezone.timedelta(minutes=settings.REGISTRATION_OTP_EXPIRY_MINUTES),
        )
    try:
        send_registration_otp_email(user, otp)
    except ValidationError:
        otp.delete()
        raise
    notify_user(
        user,
        "Verify your account",
        f"Your registration OTP is {otp.code}. It expires in {settings.REGISTRATION_OTP_EXPIRY_MINUTES} minutes.",
    )
    return otp


def verify_registration_otp(user, code):
    now = timezone.now()
    otp = (
        user.registration_otps.filter(
            code=(code or "").strip(),
            consumed_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if otp is None:
        raise ValidationError("That OTP is invalid or has expired. Please request a new code.")

    otp.mark_consumed()
    user.mark_email_verified()
    send_registration_success_email(user)
    notify_user(
        user,
        "Registration complete",
        "Your account has been verified successfully. You can now log in and start ordering.",
    )
    return user


def send_registration_otp_email(user, otp):
    try:
        send_mail(
            subject="Verify your AquaFlow account",
            message=(
                f"Hello {user.first_name},\n\n"
                "Your AquaFlow account was created successfully.\n"
                f"Use this OTP to verify your registration: {otp.code}\n"
                f"This code expires at {timezone.localtime(otp.expires_at).strftime('%Y-%m-%d %H:%M')}.\n\n"
                "If you did not create this account, you can ignore this email."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception as exc:
        logger.exception("Failed to send registration OTP email to %s", user.email)
        raise ValidationError("We could not send the verification email right now. Please try again.") from exc


def send_registration_success_email(user):
    try:
        send_mail(
            subject="Your AquaFlow registration is complete",
            message=(
                f"Hello {user.first_name},\n\n"
                "Your registration has been verified successfully. "
                "You can now log in, browse verified water companies, and place your first order."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send registration success email to %s", user.email)


def send_company_admin_activation_email(company):
    admin_users = list(get_company_admin_users(company))
    if not admin_users:
        return

    try:
        for admin_user in admin_users:
            send_mail(
                subject=f"{company.name} is now active on AquaFlow",
                message=(
                    f"Hello {admin_user.full_name or admin_user.email},\n\n"
                    f"{company.name} has passed EFDA review and is now active on AquaFlow.\n"
                    f"Login email: {admin_user.email}\n"
                    "Use the password that was configured during onboarding. If needed, request a password reset from the login page.\n\n"
                    "Next steps:\n"
                    "1. Log in to your company admin dashboard.\n"
                    "2. Add agent branches.\n"
                    "3. Add products and inventory.\n"
                    "Your company will appear to customers only after active products have stock available."
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
                recipient_list=[admin_user.email],
                fail_silently=False,
            )
    except Exception:
        logger.exception("Failed to send company admin activation email for company %s", company.pk)
