from decimal import Decimal

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import F
from django.utils import timezone

from core.models import TimeStampedModel
from accounts.validators import validate_ethiopian_phone_number


class UserRole(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    AGENT_MANAGER = "agent_manager", "Agent Manager"
    DRIVER = "driver", "Driver"
    COMPANY_ADMIN = "company_admin", "Company Admin"
    SYSTEM_ADMIN = "system_admin", "System Admin"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The email address is required.")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", UserRole.CUSTOMER)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_customer", False)
        extra_fields.setdefault("role", UserRole.SYSTEM_ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(TimeStampedModel, AbstractBaseUser, PermissionsMixin):
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True, validators=[validate_ethiopian_phone_number])
    profile_image = models.ImageField(upload_to="profiles/", blank=True, null=True)
    role = models.CharField(max_length=30, choices=UserRole.choices, default=UserRole.CUSTOMER)
    managed_company = models.ForeignKey(
        "catalog.Company",
        on_delete=models.SET_NULL,
        related_name="company_admin_users",
        blank=True,
        null=True,
    )
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_customer = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    email_verified_at = models.DateTimeField(blank=True, null=True)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(blank=True, null=True)
    last_failed_login_at = models.DateTimeField(blank=True, null=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name", "phone_number"]

    class Meta:
        ordering = ("first_name", "last_name", "email")

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        self.is_customer = self.role == UserRole.CUSTOMER
        if self.role != UserRole.COMPANY_ADMIN:
            self.managed_company = None
        if self.role == UserRole.SYSTEM_ADMIN:
            self.is_staff = True
            self.is_superuser = True
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_agent_manager(self):
        return self.role == UserRole.AGENT_MANAGER

    @property
    def is_driver(self):
        return self.role == UserRole.DRIVER

    @property
    def is_company_admin(self):
        return self.role == UserRole.COMPANY_ADMIN

    @property
    def is_system_admin(self):
        return self.role == UserRole.SYSTEM_ADMIN

    def mark_email_verified(self):
        self.is_active = True
        self.email_verified_at = timezone.now()
        self.save(update_fields=["is_active", "email_verified_at", "updated_at"])

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    def register_failed_login(self):
        now = timezone.now()
        attempts = (self.failed_login_attempts or 0) + 1
        self.last_failed_login_at = now
        if attempts >= settings.ACCOUNT_LOCKOUT_THRESHOLD:
            self.failed_login_attempts = 0
            self.locked_until = now + timezone.timedelta(minutes=settings.ACCOUNT_LOCKOUT_MINUTES)
        else:
            self.failed_login_attempts = attempts
        self.save(update_fields=["failed_login_attempts", "locked_until", "last_failed_login_at", "updated_at"])
        return self.locked_until

    def clear_login_lock(self):
        if self.failed_login_attempts or self.locked_until or self.last_failed_login_at:
            self.failed_login_attempts = 0
            self.locked_until = None
            self.last_failed_login_at = None
            self.save(update_fields=["failed_login_attempts", "locked_until", "last_failed_login_at", "updated_at"])

    def credit_wallet(self, amount):
        amount = Decimal(str(amount))
        self.__class__.objects.filter(pk=self.pk).update(
            wallet_balance=F("wallet_balance") + amount,
            updated_at=timezone.now(),
        )
        self.refresh_from_db(fields=["wallet_balance", "updated_at"])


class RegistrationOTP(TimeStampedModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="registration_otps",
    )
    email = models.EmailField()
    code = models.CharField(max_length=6)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Registration OTP for {self.email}"

    @property
    def is_valid(self):
        return self.consumed_at is None and self.expires_at > timezone.now()

    def mark_consumed(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at", "updated_at"])


class CustomerAddress(TimeStampedModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="saved_addresses",
    )
    label = models.CharField(max_length=80)
    address_line = models.TextField()
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    notes = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ("-is_default", "label", "-updated_at")
        constraints = [
            models.UniqueConstraint(fields=["user", "label"], name="unique_customer_address_label"),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.label}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            self.user.saved_addresses.exclude(pk=self.pk).update(is_default=False)

    def set_as_default(self):
        self.is_default = True
        self.save(update_fields=["is_default", "updated_at"])
