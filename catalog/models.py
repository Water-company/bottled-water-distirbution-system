import math

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
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
    logo = models.ImageField(upload_to="companies/logos/", blank=True, null=True)
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
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="managed_companies",
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


class Product(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=255)
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


class Driver(TimeStampedModel):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="drivers")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_profile",
    )
    vehicle_identifier = models.CharField(max_length=100, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("agent__name", "user__first_name")

    def __str__(self):
        return self.user.full_name or self.user.email

    @property
    def is_online(self):
        location = getattr(self.user, "driver_location", None)
        return self.is_active and bool(location and location.is_online)


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
