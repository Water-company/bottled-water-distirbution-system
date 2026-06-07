from django.core.mail import send_mail
from django.utils import timezone

from accounts.models import User
from core.models import (
    Announcement,
    AnnouncementDelivery,
    AnnouncementDeliveryStatus,
    AnnouncementTargetRole,
    AuditLog,
    Notification,
)


def notify_user(recipient, title, message, link=""):
    return Notification.objects.create(
        recipient=recipient,
        title=title,
        message=message,
        link=link,
    )


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def record_audit_log(
    *,
    request=None,
    actor=None,
    action,
    entity_type,
    entity_id="",
    entity_label="",
    old_values=None,
    new_values=None,
):
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id or ""),
        entity_label=entity_label,
        old_values=old_values or {},
        new_values=new_values or {},
        ip_address=get_client_ip(request) if request is not None else None,
    )


def get_announcement_recipients(target_role):
    users = User.objects.filter(is_active=True)
    if target_role != AnnouncementTargetRole.ALL:
        users = users.filter(role=target_role)
    return users.order_by("email")


def deliver_announcement(announcement):
    recipients = list(get_announcement_recipients(announcement.target_role))
    sent_count = 0
    failed_count = 0

    for recipient in recipients:
        notification = notify_user(
            recipient,
            announcement.title,
            announcement.message,
        )
        error_message = ""
        status = AnnouncementDeliveryStatus.SENT
        try:
            send_mail(
                subject=announcement.title,
                message=announcement.message,
                from_email=None,
                recipient_list=[recipient.email],
                fail_silently=False,
            )
            sent_count += 1
        except Exception as exc:  # pragma: no cover - safety for non-test mail backends
            status = AnnouncementDeliveryStatus.FAILED
            error_message = str(exc)
            failed_count += 1

        AnnouncementDelivery.objects.create(
            announcement=announcement,
            recipient=recipient,
            notification=notification,
            status=status,
            error_message=error_message,
            delivered_at=timezone.now() if status == AnnouncementDeliveryStatus.SENT else None,
        )

    announcement.recipient_count = len(recipients)
    announcement.sent_count = sent_count
    announcement.failed_count = failed_count
    announcement.delivered_at = timezone.now()
    announcement.save(update_fields=["recipient_count", "sent_count", "failed_count", "delivered_at", "updated_at"])
    return announcement
