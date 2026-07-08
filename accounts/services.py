import logging
import math
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
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
    if primary_admin and primary_admin.role == UserRole.COMPANY_ADMIN:
        queryset = queryset | User.objects.filter(pk=primary_admin.pk)
    return queryset.distinct()


def _generate_numeric_otp(length=6):
    upper_bound = 10 ** length
    return f"{secrets.randbelow(upper_bound):0{length}d}"


def invalidate_expired_registration_otps(user, now=None):
    now = now or timezone.now()
    return RegistrationOTP.objects.filter(
        user=user,
        consumed_at__isnull=True,
        expires_at__lte=now,
    ).update(consumed_at=F("expires_at"), updated_at=now)


def get_latest_registration_otp(user):
    return user.registration_otps.order_by("-created_at").first()


def get_registration_resend_wait_seconds(user, now=None):
    now = now or timezone.now()
    latest_otp = get_latest_registration_otp(user)
    if latest_otp is None:
        return 0

    cooldown_window = timezone.timedelta(seconds=settings.REGISTRATION_OTP_RESEND_SECONDS)
    available_at = latest_otp.created_at + cooldown_window
    remaining_seconds = math.ceil((available_at - now).total_seconds())
    return max(0, remaining_seconds)


def get_registration_otp_state(user, now=None):
    now = now or timezone.now()
    invalidate_expired_registration_otps(user, now=now)
    latest_otp = get_latest_registration_otp(user)
    active_otp = (
        user.registration_otps.filter(consumed_at__isnull=True, expires_at__gt=now)
        .order_by("-created_at")
        .first()
    )
    resend_available_at = None
    resend_wait_seconds = 0
    if latest_otp is not None:
        resend_available_at = latest_otp.created_at + timezone.timedelta(
            seconds=settings.REGISTRATION_OTP_RESEND_SECONDS
        )
        resend_wait_seconds = get_registration_resend_wait_seconds(user, now=now)

    return {
        "active_otp": active_otp,
        "latest_otp": latest_otp,
        "otp_expires_at": active_otp.expires_at if active_otp else None,
        "otp_issued_at": active_otp.created_at if active_otp else None,
        "resend_available_at": resend_available_at,
        "resend_wait_seconds": resend_wait_seconds,
        "server_now": now,
        "has_expired_otp": bool(latest_otp and active_otp is None and latest_otp.expires_at <= now),
    }


def create_registration_otp(user):
    if user.email_verified_at or user.is_active:
        raise ValidationError("This account is already verified. Please log in.")

    now = timezone.now()
    invalidate_expired_registration_otps(user, now=now)
    resend_wait_seconds = get_registration_resend_wait_seconds(user, now=now)
    if resend_wait_seconds:
        raise ValidationError(
            f"Please wait {resend_wait_seconds} second{'s' if resend_wait_seconds != 1 else ''} before requesting another OTP."
        )

    with transaction.atomic():
        RegistrationOTP.objects.filter(
            user=user,
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).update(consumed_at=now, updated_at=now)
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
    invalidate_expired_registration_otps(user, now=now)
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

    with transaction.atomic():
        otp.mark_consumed(when=now)
        user.registration_otps.filter(consumed_at__isnull=True).exclude(pk=otp.pk).update(
            consumed_at=now,
            updated_at=now,
        )
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
