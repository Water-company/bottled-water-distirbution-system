import math
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from core.models import TimeStampedModel


def _unique_slug(instance, value, field_name="slug"):
    base_slug = slugify(value)[:50] or "item"
    slug = base_slug
    model = instance.__class__
    counter = 1
    while model.objects.filter(**{field_name: slug}).exclude(pk=instance.pk).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


class CompanyVerificationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_EFDA = "pending_efda", "Pending EFDA Review"
    VERIFIED = "verified", "Verified"
    REJECTED = "rejected", "Rejected"


class Company(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    description = models.TextField()
    location = models.CharField(max_length=255)
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    efda_license_number = models.CharField(max_length=120, blank=True)
    registration_document = models.FileField(upload_to="companies/documents/", blank=True, null=True)
    logo = models.ImageField(upload_to="companies/logos/", blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    verification_status = models.CharField(
        max_length=20,
        choices=CompanyVerificationStatus.choices,
        default=CompanyVerificationStatus.DRAFT,
    )
    submitted_to_efda_at = models.DateTimeField(blank=True, null=True)
    efda_verified_at = models.DateTimeField(blank=True, null=True)
    efda_reference = models.CharField(max_length=120, blank=True)
    verification_note = models.TextField(blank=True)
    premium_feature_enabled = models.BooleanField(default=False)
    premium_streak_threshold = models.PositiveIntegerField(default=5)
    premium_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    allow_agent_credit = models.BooleanField(default=True)
    maximum_credit_duration_days = models.PositiveIntegerField(default=14)
    refunds_enabled = models.BooleanField(default=False)
    complaint_period_days = models.PositiveIntegerField(default=7)
    complaint_appeal_period_days = models.PositiveIntegerField(default=7)
    maximum_cancellation_period_minutes = models.PositiveIntegerField(default=120)
    admin = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="primary_managed_company",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name

    def clean(self):
        if self.premium_discount_percent < 0 or self.premium_discount_percent > 100:
            raise ValidationError({"premium_discount_percent": "Premium discount percent must be between 0 and 100."})
        if self.premium_streak_threshold < 1:
            raise ValidationError({"premium_streak_threshold": "Premium streak threshold must be at least 1."})
        if self.maximum_credit_duration_days < 1:
            raise ValidationError(
                {"maximum_credit_duration_days": "Maximum credit duration must be a positive integer."}
            )
        if self.maximum_cancellation_period_minutes < 1:
            raise ValidationError(
                {"maximum_cancellation_period_minutes": "Maximum cancellation period must be a positive integer."}
            )
        if self.complaint_period_days < 1:
            raise ValidationError({"complaint_period_days": "Complaint period must be at least one day."})
        if self.complaint_appeal_period_days < 1:
            raise ValidationError({"complaint_appeal_period_days": "Complaint appeal period must be at least one day."})
        if self.admin:
            if getattr(self.admin, "role", None) != "company_admin":
                raise ValidationError({"admin": "Only company admin users can be assigned as the primary company admin."})
            if self.pk and self.admin.managed_company_id != self.pk:
                raise ValidationError({"admin": "The primary company admin must belong to this company."})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self, self.name)
        if self.is_verified and self.verification_status != CompanyVerificationStatus.VERIFIED:
            self.verification_status = CompanyVerificationStatus.VERIFIED
        elif self.verification_status == CompanyVerificationStatus.VERIFIED:
            self.is_verified = True
        else:
            self.is_verified = False
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_live(self):
        return self.is_active and self.is_verified


class CompanyRefundPolicyTier(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="refund_policy_tiers")
    start_minutes = models.PositiveIntegerField()
    end_minutes = models.PositiveIntegerField(blank=True, null=True)
    refund_percent = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ("start_minutes", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "start_minutes", "end_minutes"],
                name="unique_company_refund_policy_tier",
            ),
        ]

    def __str__(self):
        end_label = self.end_minutes if self.end_minutes is not None else "up"
        return f"{self.company.name}: {self.start_minutes}-{end_label} minutes"

    def clean(self):
        if self.end_minutes is not None and self.end_minutes < self.start_minutes:
            raise ValidationError({"end_minutes": "End minute must be greater than or equal to the start minute."})
        if self.refund_percent < 0 or self.refund_percent > 100:
            raise ValidationError({"refund_percent": "Refund percentage must be between 0 and 100."})


class Product(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=255)
    size_label = models.CharField(max_length=80, blank=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    available_quantity = models.PositiveIntegerField(default=0)
    image = models.ImageField(upload_to="products/main/", blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="unique_company_product_name"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.available_quantity < 0:
            raise ValidationError({"available_quantity": "Available quantity cannot be negative."})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self, f"{self.company.name}-{self.name}")
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def in_stock(self):
        return self.available_quantity > 0


class Agent(TimeStampedModel):
    OVERDUE_STATUS_ACTIVE = "active"
    OVERDUE_STATUS_INACTIVE = "inactive"
    OVERDUE_STATUS_CHOICES = (
        (OVERDUE_STATUS_ACTIVE, "Active"),
        (OVERDUE_STATUS_INACTIVE, "Inactive"),
    )

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="agents")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    description = models.TextField(blank=True)
    location_name = models.CharField(max_length=255)
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    service_radius_km = models.DecimalField(max_digits=6, decimal_places=2, default=15)
    phone_number = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    is_accepting_orders = models.BooleanField(default=True)
    overdue_status = models.CharField(max_length=20, choices=OVERDUE_STATUS_CHOICES, default=OVERDUE_STATUS_ACTIVE)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_period_days = models.PositiveIntegerField(default=14)
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="managed_agent_branches",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("company__name", "name")
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="unique_company_agent_name"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self, f"{self.company.name}-{self.name}")
        super().save(*args, **kwargs)

    def distance_to(self, latitude, longitude):
        return haversine_km(float(self.latitude), float(self.longitude), float(latitude), float(longitude))

    def serves(self, latitude, longitude):
        return self.distance_to(latitude, longitude) <= float(self.service_radius_km)

    @property
    def is_online(self):
        return self.is_active and self.is_accepting_orders

    @property
    def outstanding_balance(self):
        return sum(
            sale.outstanding_balance
            for sale in self.batch_sales.filter(
                status__in=[AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED]
            ).select_related("batch")
        )


class Driver(TimeStampedModel):
    class AvailabilityStatus(models.TextChoices):
        AVAILABLE = "available", "Available"
        ON_DELIVERY = "on_delivery", "On Delivery"
        OFF_DUTY = "off_duty", "Off Duty"

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="drivers")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_profile",
    )
    vehicle_identifier = models.CharField(max_length=100, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.AVAILABLE,
    )

    class Meta:
        ordering = ("agent__name", "user__first_name")

    def __str__(self):
        return self.user.full_name or self.user.email

    @property
    def is_online(self):
        location = getattr(self.user, "driver_location", None)
        return self.is_active and bool(location and location.is_online)

    @property
    def can_receive_assignments(self):
        return self.is_active and self.user.is_active and self.availability_status == self.AvailabilityStatus.AVAILABLE


class AgentStock(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="stocks")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="agent_stocks")
    available_quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("agent__name", "product__name")
        constraints = [
            models.UniqueConstraint(fields=["agent", "product"], name="unique_agent_product_stock"),
        ]

    def __str__(self):
        return f"{self.agent.name} - {self.product.name}"

    def clean(self):
        if self.product.company_id != self.agent.company_id:
            raise ValidationError("Agent stock product must belong to the same company as the agent.")

    @property
    def low_stock(self):
        return self.available_quantity <= self.reorder_level


class InventoryBatch(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="inventory_batches")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="inventory_batches")
    batch_number = models.CharField(max_length=100)
    quantity_received = models.PositiveIntegerField()
    quantity_remaining = models.PositiveIntegerField()
    base_unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    expires_at = models.DateField()
    received_at = models.DateField()

    class Meta:
        ordering = ("expires_at", "received_at", "created_at")
        constraints = [
            models.UniqueConstraint(fields=["agent", "batch_number"], name="unique_agent_batch_number"),
        ]

    def __str__(self):
        return f"{self.agent.name} - {self.batch_number}"


class InventoryTransactionType(models.TextChoices):
    RESTOCK = "restock", "Restock"
    SALE = "sale", "Sale"
    RETURN = "return", "Return"
    ADJUSTMENT = "adjustment", "Adjustment"


class InventoryTransaction(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="inventory_transactions")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="inventory_transactions")
    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.SET_NULL,
        related_name="transactions",
        blank=True,
        null=True,
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="inventory_transactions",
        blank=True,
        null=True,
    )
    transaction_type = models.CharField(max_length=20, choices=InventoryTransactionType.choices)
    quantity_change = models.IntegerField()
    stock_after = models.PositiveIntegerField(default=0)
    reference = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.agent.name} - {self.product.name} ({self.get_transaction_type_display()})"


class RestockRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    FULFILLED = "fulfilled", "Fulfilled"


class RestockRequest(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="restock_requests")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="restock_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_restock_requests",
        null=True,
        blank=True,
    )
    quantity_requested = models.PositiveIntegerField()
    quantity_approved = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=RestockRequestStatus.choices, default=RestockRequestStatus.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="approved_restock_requests",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.agent.name} - {self.product.name}"


class PaymentScheduleStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PARTIAL = "partial", "Partial"
    PAID = "paid", "Paid"
    OVERDUE = "overdue", "Overdue"


class PaymentSchedule(TimeStampedModel):
    restock_request = models.ForeignKey(RestockRequest, on_delete=models.CASCADE, related_name="payment_schedules")
    due_date = models.DateField()
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    excise_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    transport_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=PaymentScheduleStatus.choices, default=PaymentScheduleStatus.PENDING)

    class Meta:
        ordering = ("due_date", "-created_at")

    def __str__(self):
        return f"Schedule for {self.restock_request}"

    @property
    def total_amount(self):
        return self.base_price + self.excise_tax + self.vat + self.transport_cost


class CompanyBatchStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    RECALLED = "recalled", "Recalled"
    CLOSED = "closed", "Closed"


class AgentBatchSalePaymentType(models.TextChoices):
    FULL = "full", "Full Payment"
    PARTIAL = "partial", "Partial Payment"
    CREDIT = "credit", "On Credit"


class AgentBatchSaleStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Rejected"


class AgentBatchSalePaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending Confirmation"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"


class AgentBatchSaleCheckoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class CompanyBatch(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="production_batches")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="production_batches")
    batch_number = models.CharField(max_length=100)
    production_date = models.DateField()
    total_cases_produced = models.PositiveIntegerField()
    unsold_cases_remaining = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=CompanyBatchStatus.choices, default=CompanyBatchStatus.AVAILABLE)
    recall_reason = models.TextField(blank=True)
    recalled_cases = models.PositiveIntegerField(default=0)
    recalled_at = models.DateTimeField(blank=True, null=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_company_batches",
        blank=True,
        null=True,
    )
    recalled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recalled_company_batches",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("-production_date", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=["company", "batch_number"], name="unique_company_batch_number"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.batch_number}"

    def clean(self):
        if not self.company_id or not self.product_id:
            return
        if self.product.company_id != self.company_id:
            raise ValidationError({"product": "Batch product must belong to the same company."})
        if self.total_cases_produced < 1:
            raise ValidationError({"total_cases_produced": "Batch quantity must be at least one case."})
        if self.unsold_cases_remaining > self.total_cases_produced:
            raise ValidationError({"unsold_cases_remaining": "Unsold cases cannot exceed produced cases."})
        if self.recalled_cases > self.total_cases_produced:
            raise ValidationError({"recalled_cases": "Recalled cases cannot exceed produced cases."})

    def save(self, *args, **kwargs):
        if self._state.adding and not self.unsold_cases_remaining:
            self.unsold_cases_remaining = self.total_cases_produced
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def cases_sold(self):
        return max(self.total_cases_produced - self.unsold_cases_remaining, 0)

    @property
    def days_since_production(self):
        return max((timezone.localdate() - self.production_date).days + 1, 1)

    @property
    def sales_velocity_per_day(self):
        return self.cases_sold / self.days_since_production

    @property
    def can_allocate(self):
        return self.status == CompanyBatchStatus.AVAILABLE and self.unsold_cases_remaining > 0

    @property
    def cash_recovery_rate(self):
        tracked_statuses = [AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED]
        total_owed = sum(sale.total_amount for sale in self.agent_sales.filter(status__in=tracked_statuses))
        if total_owed <= 0:
            return 0
        total_collected = sum(sale.amount_collected for sale in self.agent_sales.filter(status__in=tracked_statuses))
        return round((total_collected / total_owed) * 100, 2)


class AgentBatchSale(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="batch_sales")
    batch = models.ForeignKey(CompanyBatch, on_delete=models.PROTECT, related_name="agent_sales")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="requested_batch_sales",
        blank=True,
        null=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="approved_batch_sales",
        blank=True,
        null=True,
    )
    quantity_requested = models.PositiveIntegerField()
    quantity_approved = models.PositiveIntegerField(default=0)
    quantity_received = models.PositiveIntegerField(default=0)
    payment_type = models.CharField(max_length=20, choices=AgentBatchSalePaymentType.choices)
    requested_upfront_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    credit_terms_days = models.PositiveIntegerField(blank=True, null=True)
    credit_due_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=AgentBatchSaleStatus.choices, default=AgentBatchSaleStatus.PENDING)
    requested_note = models.TextField(blank=True)
    decision_note = models.TextField(blank=True)
    receipt_note = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    received_at = models.DateTimeField(blank=True, null=True)
    rejected_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    overdue_notified_at = models.DateTimeField(blank=True, null=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="received_batch_sales",
        blank=True,
        null=True,
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="cancelled_batch_sales",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.agent.name} - {self.batch.batch_number}"

    def clean(self):
        if not self.agent_id or not self.batch_id:
            return
        if self.batch.company_id != self.agent.company_id:
            raise ValidationError("Agents can only request batches from their own company.")
        if self.quantity_requested < 1:
            raise ValidationError({"quantity_requested": "Request at least one case."})
        if self.quantity_approved and self.quantity_approved > self.batch.total_cases_produced:
            raise ValidationError({"quantity_approved": "Approved cases cannot exceed the batch total."})
        if self.quantity_received and self.quantity_received > self.quantity_approved:
            raise ValidationError({"quantity_received": "Received cases cannot exceed approved cases."})
        if self.requested_upfront_amount < 0:
            raise ValidationError({"requested_upfront_amount": "Upfront payment cannot be negative."})
        if self.credit_terms_days is not None and self.credit_terms_days < 1:
            raise ValidationError({"credit_terms_days": "Credit terms must be at least one day."})
        if self.status == AgentBatchSaleStatus.RECEIVED:
            if self.quantity_received < 1:
                raise ValidationError({"quantity_received": "Received sales must record at least one case."})
            if self.quantity_received != self.quantity_approved:
                raise ValidationError({"quantity_received": "Full receipt is required for received sales."})
            if not self.received_at:
                raise ValidationError({"received_at": "Received sales must record when the stock was confirmed."})
        if self.status == AgentBatchSaleStatus.CANCELLED and not self.cancellation_reason.strip():
            raise ValidationError({"cancellation_reason": "Cancelled sales require a cancellation reason."})

    @property
    def total_amount(self):
        return self.unit_price * self.quantity_approved

    @property
    def amount_collected(self):
        return (
            self.payments.filter(status=AgentBatchSalePaymentStatus.CONFIRMED)
            .aggregate(total=models.Sum("amount"))
            .get("total")
            or 0
        )

    @property
    def outstanding_balance(self):
        if self.status in {AgentBatchSaleStatus.REJECTED, AgentBatchSaleStatus.CANCELLED}:
            return Decimal("0.00")
        return max(self.total_amount - self.amount_collected, 0)

    @property
    def collection_status(self):
        if self.status == AgentBatchSaleStatus.CANCELLED:
            return "cancelled"
        if self.status == AgentBatchSaleStatus.REJECTED:
            return "rejected"
        if self.status not in {AgentBatchSaleStatus.APPROVED, AgentBatchSaleStatus.RECEIVED}:
            return "pending"
        if self.outstanding_balance <= 0:
            return "paid"
        if self.status == AgentBatchSaleStatus.APPROVED and self.payment_type == AgentBatchSalePaymentType.FULL:
            return "awaiting_payment"
        if self.status == AgentBatchSaleStatus.RECEIVED and self.credit_due_date and self.credit_due_date < timezone.localdate():
            return "overdue"
        if self.amount_collected > 0:
            return "partial"
        return "awaiting_receipt" if self.status == AgentBatchSaleStatus.APPROVED else "unpaid"

    @property
    def is_overdue(self):
        return self.collection_status == "overdue"

    @property
    def can_confirm_receipt(self):
        if self.status != AgentBatchSaleStatus.APPROVED:
            return False
        if self.payment_type == AgentBatchSalePaymentType.FULL and self.outstanding_balance > 0:
            return False
        return True


class AgentBatchSalePayment(TimeStampedModel):
    sale = models.ForeignKey(AgentBatchSale, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="submitted_agent_batch_payments",
        blank=True,
        null=True,
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="confirmed_agent_batch_payments",
        blank=True,
        null=True,
    )
    status = models.CharField(
        max_length=20,
        choices=AgentBatchSalePaymentStatus.choices,
        default=AgentBatchSalePaymentStatus.PENDING,
    )
    submitted_note = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.sale.batch.batch_number} payment - {self.amount}"

    def clean(self):
        if self.amount <= 0:
            raise ValidationError({"amount": "Payment amount must be greater than zero."})


class AgentBatchSaleCheckout(TimeStampedModel):
    sale = models.OneToOneField(AgentBatchSale, on_delete=models.CASCADE, related_name="checkout")
    tx_ref = models.CharField(max_length=120, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=20,
        choices=AgentBatchSaleCheckoutStatus.choices,
        default=AgentBatchSaleCheckoutStatus.PENDING,
    )
    checkout_url = models.URLField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.sale.batch.batch_number} checkout - {self.tx_ref}"


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="gallery")
    image = models.ImageField(upload_to="products/gallery/")
    alt_text = models.CharField(max_length=255, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ("-is_primary", "created_at")

    def __str__(self):
        return f"{self.product.name} image"


def haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371
    lat1_radians = math.radians(lat1)
    lon1_radians = math.radians(lon1)
    lat2_radians = math.radians(lat2)
    lon2_radians = math.radians(lon2)

    delta_lat = lat2_radians - lat1_radians
    delta_lon = lon2_radians - lon1_radians

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_radians) * math.cos(lat2_radians) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c
