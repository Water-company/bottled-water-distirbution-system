import random
import secrets
import string
import json
from io import BytesIO
from decimal import Decimal

import qrcode
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from orders.qr_tokens import build_customer_token_id, build_signed_qr_token


class LocationSource(models.TextChoices):
    CURRENT = "current", "Current Location"
    MAP = "map", "Chosen on Map"


class OrderStatus(models.TextChoices):
    REQUESTED = "requested", "Waiting for Agent Response"
    REJECTED = "rejected", "Rejected by Agent"
    PAYMENT_PENDING = "payment_pending", "Accepted - Payment Required"
    PAID = "paid", "Paid - Preparing Delivery"
    DRIVER_ASSIGNED = "driver_assigned", "Assigned"
    DRIVER_ACCEPTED = "driver_accepted", "Accepted"
    PICKED_UP = "picked_up", "Picked Up"
    OUT_FOR_DELIVERY = "out_for_delivery", "On the Way"
    ARRIVED = "arrived", "Arrived"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class AgentRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class PaymentProvider(models.TextChoices):
    CHAPA = "chapa", "Chapa"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    PARTIALLY_REFUNDED = "partially_refunded", "Partially Refunded"
    REFUNDED = "refunded", "Refunded"


class RefundRequestType(models.TextChoices):
    CANCELLATION = "cancellation", "Cancellation Refund"
    SERVICE_ISSUE = "service_issue", "Service Issue Refund"


class RefundRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending Review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class RefundPayoutMethod(models.TextChoices):
    GATEWAY = "gateway", "Refund via Gateway"
    WALLET_CREDIT = "wallet_credit", "Wallet Credit"


class DeliveryIssueType(models.TextChoices):
    CUSTOMER_NOT_FOUND = "customer_not_found", "Customer not found"
    WRONG_ADDRESS = "wrong_address", "Wrong address"
    TRAFFIC_DELAY = "traffic_delay", "Traffic / delay"
    VEHICLE_ISSUE = "vehicle_issue", "Vehicle issue"
    OTHER = "other", "Other"


class Order(TimeStampedModel):
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders")
    company = models.ForeignKey("catalog.Company", on_delete=models.PROTECT, related_name="orders")
    selected_agent = models.ForeignKey(
        "catalog.Agent",
        on_delete=models.SET_NULL,
        related_name="accepted_orders",
        blank=True,
        null=True,
    )
    assigned_driver = models.ForeignKey(
        "catalog.Driver",
        on_delete=models.SET_NULL,
        related_name="assigned_orders",
        blank=True,
        null=True,
    )
    order_number = models.CharField(max_length=12, unique=True, editable=False)
    status = models.CharField(max_length=30, choices=OrderStatus.choices, default=OrderStatus.REQUESTED)
    location_source = models.CharField(max_length=20, choices=LocationSource.choices, default=LocationSource.MAP)
    delivery_address = models.TextField()
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    phone_number = models.CharField(max_length=20)
    notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    premium_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    premium_streak_count = models.PositiveIntegerField(default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    accepted_at = models.DateTimeField(blank=True, null=True)
    rejected_at = models.DateTimeField(blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    driver_assigned_at = models.DateTimeField(blank=True, null=True)
    driver_accepted_at = models.DateTimeField(blank=True, null=True)
    picked_up_at = models.DateTimeField(blank=True, null=True)
    out_for_delivery_at = models.DateTimeField(blank=True, null=True)
    arrived_at = models.DateTimeField(blank=True, null=True)
    delivered_at = models.DateTimeField(blank=True, null=True)
    failed_at = models.DateTimeField(blank=True, null=True)
    agent_response_deadline = models.DateTimeField(blank=True, null=True)
    cancellation_deadline = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.order_number

    @staticmethod
    def generate_order_number():
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        return f"ORD-{suffix}"

    def save(self, *args, **kwargs):
        previous_status = None
        is_new = self._state.adding
        if not self.order_number:
            candidate = self.generate_order_number()
            while Order.objects.filter(order_number=candidate).exists():
                candidate = self.generate_order_number()
            self.order_number = candidate

        if not is_new and self.pk:
            previous_status = Order.objects.filter(pk=self.pk).values_list("status", flat=True).first()

        self.full_clean()
        super().save(*args, **kwargs)

        if is_new or previous_status != self.status:
            OrderStatusHistory.objects.create(order=self, status=self.status)

    @property
    def last_status_update(self):
        latest = self.status_history.order_by("-created_at").first()
        return latest.created_at if latest else self.updated_at

    @property
    def can_make_payment(self):
        return self.status == OrderStatus.PAYMENT_PENDING

    @property
    def delivery_confirmation(self):
        return getattr(self, "confirmation", None)

    @property
    def cancellation_window_open(self):
        return bool(self.cancellation_deadline and timezone.now() <= self.cancellation_deadline)

    @property
    def can_cancel(self):
        if self.status in {OrderStatus.REQUESTED, OrderStatus.PAYMENT_PENDING}:
            return True
        if self.status in {OrderStatus.PAID, OrderStatus.DRIVER_ASSIGNED, OrderStatus.DRIVER_ACCEPTED}:
            return self.cancellation_window_open
        return False

    @property
    def refund_deadline(self):
        if not self.delivered_at:
            return None
        return self.delivered_at + timezone.timedelta(days=settings.ORDER_REFUND_REQUEST_WINDOW_DAYS)

    @property
    def can_request_refund(self):
        payment = getattr(self, "payment", None)
        if self.status != OrderStatus.DELIVERED or not payment:
            return False
        if payment.status in {PaymentStatus.CANCELLED, PaymentStatus.REFUNDED}:
            return False
        if self.refund_deadline is None:
            return False
        return timezone.now() <= self.refund_deadline

    @property
    def feedback_record(self):
        return getattr(self, "feedback", None)

    @property
    def can_leave_feedback(self):
        return self.status == OrderStatus.DELIVERED and not hasattr(self, "feedback")

    def clean(self):
        if self.latitude is not None and not Decimal("-90") <= Decimal(self.latitude) <= Decimal("90"):
            raise ValidationError({"latitude": "Latitude must be between -90 and 90."})
        if self.longitude is not None and not Decimal("-180") <= Decimal(self.longitude) <= Decimal("180"):
            raise ValidationError({"longitude": "Longitude must be between -180 and 180."})
        if self.subtotal is not None and self.subtotal < 0:
            raise ValidationError({"subtotal": "Subtotal cannot be negative."})
        if self.delivery_fee is not None and self.delivery_fee < 0:
            raise ValidationError({"delivery_fee": "Delivery fee cannot be negative."})
        if self.total is not None and self.total < 0:
            raise ValidationError({"total": "Total cannot be negative."})


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="order_items")
    product_name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField()

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

    def save(self, *args, **kwargs):
        if not self.product_name:
            self.product_name = self.product.name
        if not self.unit_price:
            self.unit_price = self.product.price
        super().save(*args, **kwargs)

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class OrderAgentRequest(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="agent_requests")
    agent = models.ForeignKey("catalog.Agent", on_delete=models.CASCADE, related_name="order_requests")
    status = models.CharField(max_length=20, choices=AgentRequestStatus.choices, default=AgentRequestStatus.PENDING)
    distance_km = models.DecimalField(max_digits=8, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    responded_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("distance_km", "created_at")
        constraints = [
            models.UniqueConstraint(fields=["order", "agent"], name="unique_order_agent_request"),
        ]

    def __str__(self):
        return f"{self.order.order_number} -> {self.agent.name}"


class Payment(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="payment")
    provider = models.CharField(max_length=20, choices=PaymentProvider.choices, default=PaymentProvider.CHAPA)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=100, unique=True)
    checkout_url = models.URLField(blank=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.order.order_number} payment"

    @property
    def refundable_amount(self):
        if self.status == PaymentStatus.REFUNDED:
            return 0
        if self.status == PaymentStatus.CANCELLED:
            return 0
        return self.amount


class DeliveryConfirmation(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="confirmation")
    qr_token = models.CharField(max_length=512, unique=True, editable=False)
    qr_code_image = models.ImageField(upload_to="orders/qr_codes/", blank=True, null=True)
    otp_code = models.CharField(max_length=6, editable=False)
    expires_at = models.DateTimeField(blank=True, null=True)
    scanned_at = models.DateTimeField(blank=True, null=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="scanned_delivery_confirmations",
        blank=True,
        null=True,
    )
    verified_at = models.DateTimeField(blank=True, null=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="verified_deliveries",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Confirmation for {self.order.order_number}"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(hours=settings.QR_TOKEN_EXPIRY_HOURS)
        if not self.qr_token:
            self.qr_token = self._generate_signed_qr_token()
        if not self.otp_code:
            self.otp_code = "".join(random.choices(string.digits, k=6))
        if not self.qr_code_image:
            self._generate_qr_code_image()
        super().save(*args, **kwargs)

    @property
    def customer_token_id(self):
        return build_customer_token_id(self.order.customer_id)

    @property
    def qr_payload(self):
        return {
            "order_id": self.order.order_number,
            "customer_id": self.customer_token_id,
            "token": self.qr_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else "",
        }

    @property
    def qr_payload_json(self):
        return json.dumps(self.qr_payload, separators=(",", ":"))

    @property
    def is_expired(self):
        return bool(self.expires_at and timezone.now() >= self.expires_at)

    def refresh_qr_assets(self, save=True):
        self.expires_at = timezone.now() + timezone.timedelta(hours=settings.QR_TOKEN_EXPIRY_HOURS)
        self.qr_token = self._generate_signed_qr_token()
        self.scanned_at = None
        self.scanned_by = None
        self.verified_at = None
        self.verified_by = None
        self._generate_qr_code_image()
        if save:
            self.save(
                update_fields=[
                    "qr_token",
                    "qr_code_image",
                    "expires_at",
                    "scanned_at",
                    "scanned_by",
                    "verified_at",
                    "verified_by",
                    "updated_at",
                ]
            )
        return self

    def _generate_signed_qr_token(self):
        return build_signed_qr_token(
            order_id=self.order.order_number,
            customer_id=self.customer_token_id,
            expires_at=self.expires_at,
            nonce=secrets.token_hex(8),
        )

    def _generate_qr_code_image(self):
        qr_code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
        qr_code.add_data(self.qr_payload_json)
        qr_code.make(fit=True)
        image = qr_code.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        filename = f"{self.order.order_number.lower()}-confirmation.png"
        self.qr_code_image.save(filename, ContentFile(buffer.getvalue()), save=False)


class DeliveryIssue(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="delivery_issues")
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reported_delivery_issues",
        blank=True,
        null=True,
    )
    issue_type = models.CharField(max_length=40, choices=DeliveryIssueType.choices)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.order.order_number} - {self.get_issue_type_display()}"


class DeliveryFeedback(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="feedback")
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="delivery_feedback_entries",
    )
    driver = models.ForeignKey(
        "catalog.Driver",
        on_delete=models.SET_NULL,
        related_name="delivery_feedback_entries",
        blank=True,
        null=True,
    )
    rating = models.PositiveSmallIntegerField(blank=True, null=True)
    comment = models.TextField(blank=True)
    photo = models.ImageField(upload_to="orders/feedback/", blank=True, null=True)
    skipped_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Feedback for {self.order.order_number}"

    def clean(self):
        if self.rating is not None and not 1 <= self.rating <= 5:
            raise ValidationError({"rating": "Rating must be between 1 and 5."})
        if self.skipped_at and self.rating is not None:
            raise ValidationError("Skipped feedback cannot also have a rating.")

    @property
    def was_skipped(self):
        return self.skipped_at is not None


class OrderStatusHistory(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=30, choices=OrderStatus.choices)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        verbose_name_plural = "order status history"

    def __str__(self):
        return f"{self.order.order_number} - {self.get_status_display()}"


class RefundRequest(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="refund_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="requested_refunds",
        blank=True,
        null=True,
    )
    request_type = models.CharField(max_length=30, choices=RefundRequestType.choices)
    status = models.CharField(max_length=20, choices=RefundRequestStatus.choices, default=RefundRequestStatus.PENDING)
    payout_method = models.CharField(max_length=20, choices=RefundPayoutMethod.choices, default=RefundPayoutMethod.GATEWAY)
    reason = models.TextField(blank=True)
    requested_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    approved_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_refunds",
        blank=True,
        null=True,
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="processed_refunds",
        blank=True,
        null=True,
    )
    resolution_note = models.TextField(blank=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.order.order_number} {self.get_request_type_display()}"


class RefundEvidence(TimeStampedModel):
    refund_request = models.ForeignKey(RefundRequest, on_delete=models.CASCADE, related_name="evidences")
    image = models.ImageField(upload_to="orders/refunds/")

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"Evidence for {self.refund_request.order.order_number}"
