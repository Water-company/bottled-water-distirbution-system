from django.db import models
from django.conf import settings


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Notification(TimeStampedModel):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    link = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.recipient.email} - {self.title}"


class AnnouncementTargetRole(models.TextChoices):
    ALL = "all", "All Roles"
    CUSTOMER = "customer", "Customer"
    AGENT_MANAGER = "agent_manager", "Agent Manager"
    DRIVER = "driver", "Driver"
    COMPANY_ADMIN = "company_admin", "Company Admin"
    SYSTEM_ADMIN = "system_admin", "System Admin"


class Announcement(TimeStampedModel):
    title = models.CharField(max_length=255)
    message = models.TextField()
    target_role = models.CharField(max_length=30, choices=AnnouncementTargetRole.choices, default=AnnouncementTargetRole.ALL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_announcements",
        blank=True,
        null=True,
    )
    recipient_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    delivered_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.title


class AnnouncementDeliveryStatus(models.TextChoices):
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class AnnouncementDelivery(TimeStampedModel):
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="deliveries")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="announcement_deliveries")
    notification = models.ForeignKey(
        Notification,
        on_delete=models.SET_NULL,
        related_name="announcement_deliveries",
        blank=True,
        null=True,
    )
    status = models.CharField(max_length=20, choices=AnnouncementDeliveryStatus.choices, default=AnnouncementDeliveryStatus.SENT)
    error_message = models.TextField(blank=True)
    delivered_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["announcement", "recipient"], name="unique_announcement_recipient"),
        ]

    def __str__(self):
        return f"{self.announcement.title} -> {self.recipient.email}"


class AuditLog(TimeStampedModel):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        blank=True,
        null=True,
    )
    action = models.CharField(max_length=120)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80, blank=True)
    entity_label = models.CharField(max_length=255, blank=True)
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.action} - {self.entity_type} {self.entity_label or self.entity_id}"


class DriverLocation(TimeStampedModel):
    driver_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_location",
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    is_online = models.BooleanField(default=False)
    last_ping_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_ping_at",)

    def __str__(self):
        return f"{self.driver_user.email} location"

# Create your models here.
