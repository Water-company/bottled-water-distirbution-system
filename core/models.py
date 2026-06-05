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
