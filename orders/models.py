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


class ComplaintCategory(models.TextChoices):
    MISSING_ITEMS = "missing_items", "Missing items"
    INCORRECT_QUANTITY = "incorrect_quantity", "Incorrect quantity"
    WRONG_PRODUCTS = "wrong_products", "Wrong products"
    DAMAGED_PRODUCTS = "damaged_products", "Damaged products"
    POOR_QUALITY = "poor_quality", "Poor product quality"
    OTHER = "other", "Other issues"


class ComplaintStatus(models.TextChoices):
    SUBMITTED = "submitted", "Complaint Submitted"
    AWAITING_AGENT_RESPONSE = "awaiting_agent_response", "Awaiting Agent Response"
    UNDER_COMPANY_REVIEW = "under_company_review", "Under Company Review"
    DECISION_ISSUED = "decision_issued", "Decision Issued"
    APPEAL_SUBMITTED = "appeal_submitted", "Appeal Submitted"
    UNDER_SYSTEM_REVIEW = "under_system_review", "Under System Review"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


class ComplaintAgentResponseType(models.TextChoices):
    ACCEPT_RESPONSIBILITY = "accept_responsibility", "Accept responsibility"
    DISPUTE = "dispute", "Dispute the complaint"
    ADDITIONAL_INFORMATION = "additional_information", "Provide additional information"


class ComplaintResolutionType(models.TextChoices):
    FULL_REFUND = "full_refund", "Full refund"
    PARTIAL_REFUND = "partial_refund", "Partial refund"
    REPLACEMENT_DELIVERY = "replacement_delivery", "Replacement delivery"
    ADDITIONAL_DELIVERY = "additional_delivery", "Additional delivery for missing products"
    REJECTED = "rejected", "Complaint rejected"


class RefundTransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


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
        indexes = [
            models.Index(fields=["status"], name="orders_order_status_idx"),
        ]

    def __str__(self):
        return self.order_number

    @staticmethod
    def generate_order_number():
        alphabet = string.ascii_uppercase + string.digits
        suffix = "".join(secrets.choice(alphabet) for _ in range(8))
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
    def complaint_deadline(self):
        if not self.delivered_at:
            return None
        complaint_period_days = getattr(self.company, "complaint_period_days", 0) or 0
        if complaint_period_days < 1:
            return None
        return self.delivered_at + timezone.timedelta(days=complaint_period_days)

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
    def can_submit_complaint(self):
        payment = getattr(self, "payment", None)
        if self.status != OrderStatus.DELIVERED or not payment:
            return False
        if payment.status in {PaymentStatus.CANCELLED}:
            return False
        if self.complaint_deadline is None:
            return False
        return timezone.now() <= self.complaint_deadline

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


class OrderLiveTracking(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="live_tracking")
    driver = models.ForeignKey(
        "catalog.Driver",
        on_delete=models.SET_NULL,
        related_name="live_trackings",
        blank=True,
        null=True,
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    recorded_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    paused_at = models.DateTimeField(blank=True, null=True)
    resumed_at = models.DateTimeField(blank=True, null=True)
    stopped_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=False)
    is_paused = models.BooleanField(default=False)
    last_distance_meters = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    last_eta_minutes = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        ordering = ("-recorded_at", "-updated_at")
        indexes = [
            models.Index(fields=["is_active"], name="ord_live_active_idx"),
            models.Index(fields=["recorded_at"], name="ord_live_recorded_idx"),
        ]

    def __str__(self):
        return f"Tracking for {self.order.order_number}"

    def clean(self):
        if self.latitude is not None and not Decimal("-90") <= Decimal(self.latitude) <= Decimal("90"):
            raise ValidationError({"latitude": "Latitude must be between -90 and 90."})
        if self.longitude is not None and not Decimal("-180") <= Decimal(self.longitude) <= Decimal("180"):
            raise ValidationError({"longitude": "Longitude must be between -180 and 180."})


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
        from core.policies import quantize_money

        if self.status == PaymentStatus.REFUNDED:
            return 0
        if self.status == PaymentStatus.CANCELLED:
            return 0
        refunded_total = (
            self.refund_transactions.filter(status=RefundTransactionStatus.SUCCEEDED)
            .aggregate(total=models.Sum("amount"))
            .get("total")
            or Decimal("0.00")
        )
        remaining = quantize_money(self.amount - refunded_total)
        return remaining if remaining > 0 else Decimal("0.00")


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
            self.otp_code = f"{secrets.randbelow(10 ** 6):06d}"
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


class RefundRequestStatusHistory(models.Model):
    refund_request = models.ForeignKey(RefundRequest, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=20, choices=RefundRequestStatus.choices)
    title = models.CharField(max_length=80)
    note = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="refund_status_updates",
        blank=True,
        null=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        verbose_name_plural = "refund request status history"

    def __str__(self):
        return f"{self.refund_request.order.order_number} refund - {self.title}"


class RefundEvidence(TimeStampedModel):
    refund_request = models.ForeignKey(RefundRequest, on_delete=models.CASCADE, related_name="evidences")
    image = models.ImageField(upload_to="orders/refunds/")

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"Evidence for {self.refund_request.order.order_number}"


class Complaint(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="complaints")
    reference_number = models.CharField(max_length=24, unique=True, editable=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="submitted_complaints",
        blank=True,
        null=True,
    )
    category = models.CharField(max_length=30, choices=ComplaintCategory.choices)
    description = models.TextField()
    status = models.CharField(
        max_length=30,
        choices=ComplaintStatus.choices,
        default=ComplaintStatus.SUBMITTED,
    )
    resolution_type = models.CharField(
        max_length=30,
        choices=ComplaintResolutionType.choices,
        blank=True,
    )
    resolution_note = models.TextField(blank=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    linked_refund_request = models.OneToOneField(
        RefundRequest,
        on_delete=models.SET_NULL,
        related_name="complaint",
        blank=True,
        null=True,
    )
    agent_response_type = models.CharField(
        max_length=30,
        choices=ComplaintAgentResponseType.choices,
        blank=True,
    )
    agent_response_note = models.TextField(blank=True)
    agent_responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="agent_responded_complaints",
        blank=True,
        null=True,
    )
    agent_responded_at = models.DateTimeField(blank=True, null=True)
    agent_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="agent_reviewed_complaints",
        blank=True,
        null=True,
    )
    agent_reviewed_at = models.DateTimeField(blank=True, null=True)
    agent_note = models.TextField(blank=True)
    company_decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="company_decided_complaints",
        blank=True,
        null=True,
    )
    company_decided_at = models.DateTimeField(blank=True, null=True)
    company_decision_reason = models.TextField(blank=True)
    final_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="final_reviewed_complaints",
        blank=True,
        null=True,
    )
    final_reviewed_at = models.DateTimeField(blank=True, null=True)
    final_decision_reason = models.TextField(blank=True)
    appeal_reason = models.TextField(blank=True)
    appealed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="appealed_complaints",
        blank=True,
        null=True,
    )
    appealed_at = models.DateTimeField(blank=True, null=True)
    system_decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="system_decided_complaints",
        blank=True,
        null=True,
    )
    system_decided_at = models.DateTimeField(blank=True, null=True)
    system_decision_reason = models.TextField(blank=True)
    resolved_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.reference_number

    @staticmethod
    def generate_reference_number():
        alphabet = string.ascii_uppercase + string.digits
        suffix = "".join(secrets.choice(alphabet) for _ in range(10))
        return f"CMP-{suffix}"

    def save(self, *args, **kwargs):
        if not self.reference_number:
            candidate = self.generate_reference_number()
            while Complaint.objects.filter(reference_number=candidate).exists():
                candidate = self.generate_reference_number()
            self.reference_number = candidate
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        if self.refund_amount < 0:
            raise ValidationError({"refund_amount": "Refund amount cannot be negative."})

    @property
    def appeal_deadline(self):
        if not self.company_decided_at:
            return None
        appeal_period_days = getattr(self.order.company, "complaint_appeal_period_days", 0) or 0
        if appeal_period_days < 1:
            return None
        return self.company_decided_at + timezone.timedelta(days=appeal_period_days)

    @property
    def can_customer_appeal(self):
        if self.status != ComplaintStatus.DECISION_ISSUED:
            return False
        if self.appealed_at or self.system_decided_at:
            return False
        deadline = self.appeal_deadline
        if deadline is None:
            return False
        return timezone.now() <= deadline

    @property
    def appeal_window_expired(self):
        deadline = self.appeal_deadline
        return bool(deadline and timezone.now() > deadline)


class ComplaintStatusHistory(models.Model):
    complaint = models.ForeignKey(Complaint, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=30, choices=ComplaintStatus.choices)
    title = models.CharField(max_length=80)
    note = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="complaint_status_updates",
        blank=True,
        null=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        verbose_name_plural = "complaint status history"

    def __str__(self):
        return f"{self.complaint.reference_number} - {self.title}"


class ComplaintEvidence(TimeStampedModel):
    complaint = models.ForeignKey(Complaint, on_delete=models.CASCADE, related_name="evidences")
    image = models.FileField(upload_to="orders/complaints/")

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"Evidence for {self.complaint.reference_number}"

    @property
    def filename(self):
        return (self.image.name or "").split("/")[-1]

    @property
    def is_image(self):
        file_name = (self.image.name or "").lower()
        return file_name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"))


class RefundTransaction(TimeStampedModel):
    refund_request = models.ForeignKey(
        RefundRequest,
        on_delete=models.CASCADE,
        related_name="transactions",
        blank=True,
        null=True,
    )
    complaint = models.ForeignKey(
        Complaint,
        on_delete=models.SET_NULL,
        related_name="refund_transactions",
        blank=True,
        null=True,
    )
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="refund_transactions")
    provider = models.CharField(max_length=20, choices=PaymentProvider.choices, default=PaymentProvider.CHAPA)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=20,
        choices=RefundTransactionStatus.choices,
        default=RefundTransactionStatus.PENDING,
    )
    provider_reference = models.CharField(max_length=120, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recorded_refund_transactions",
        blank=True,
        null=True,
    )
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.payment.order.order_number} refund {self.amount}"


class SupportActionLog(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="support_action_logs")
    refund_request = models.ForeignKey(
        RefundRequest,
        on_delete=models.SET_NULL,
        related_name="support_logs",
        blank=True,
        null=True,
    )
    complaint = models.ForeignKey(
        Complaint,
        on_delete=models.SET_NULL,
        related_name="support_logs",
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="support_action_logs",
        blank=True,
        null=True,
    )
    action = models.CharField(max_length=80)
    details = models.TextField(blank=True)
    outcome = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"{self.order.order_number} - {self.action}"
