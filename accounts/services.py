import random
import string

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.utils import timezone

from accounts.models import RegistrationOTP
from core.services import notify_user


def _generate_numeric_otp(length=6):
    return "".join(random.choices(string.digits, k=length))


def create_registration_otp(user):
    now = timezone.now()
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
    send_registration_otp_email(user, otp)
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
        fail_silently=True,
    )


def send_registration_success_email(user):
    send_mail(
        subject="Your AquaFlow registration is complete",
        message=(
            f"Hello {user.first_name},\n\n"
            "Your registration has been verified successfully. "
            "You can now log in, browse verified water companies, and place your first order."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[user.email],
        fail_silently=True,
    )
