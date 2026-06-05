from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


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
    phone_validator = RegexValidator(
        regex=r"^\+?[\d\s-]{9,20}$",
        message="Enter a valid phone number.",
    )

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True, validators=[phone_validator])
    profile_image = models.ImageField(upload_to="profiles/", blank=True, null=True)
    role = models.CharField(max_length=30, choices=UserRole.choices, default=UserRole.CUSTOMER)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_customer = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    email_verified_at = models.DateTimeField(blank=True, null=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name", "phone_number"]

    class Meta:
        ordering = ("first_name", "last_name", "email")

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        self.is_customer = self.role == UserRole.CUSTOMER
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
