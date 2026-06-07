from django import forms
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomerAddress, User, UserRole
from catalog.models import (
    Agent,
    AgentBatchSale,
    AgentBatchSalePayment,
    AgentBatchSalePaymentType,
    AgentStock,
    CompanyBatch,
    CompanyBatchStatus,
    Company,
    Driver,
    InventoryTransactionType,
    PaymentSchedule,
    PaymentScheduleStatus,
    Product,
    RestockRequest,
)
from core.models import DriverLocation
from core.models import Announcement, AnnouncementTargetRole
from orders.models import DeliveryIssueType


class RegistrationForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput, label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm Password")

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone_number")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"]
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("An account with this phone number already exists.")
        return phone_number

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Passwords do not match.")

        if password1:
            password_validation.validate_password(password1)

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = user.email.lower()
        user.is_active = False
        user.email_verified_at = None
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class RegistrationOTPForm(forms.Form):
    email = forms.EmailField()
    otp_code = forms.CharField(max_length=6, label="OTP Code")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_email(self):
        return self.cleaned_data["email"].lower()


class LoginForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)
    remember_me = forms.BooleanField(required=False)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "remember_me" else "form-control"

    def clean(self):
        cleaned_data = super().clean()
        email = (cleaned_data.get("email") or "").lower()
        password = cleaned_data.get("password")

        if email and password:
            cleaned_data["email"] = email
            existing_user = User.objects.filter(email__iexact=email).first()
            if existing_user and existing_user.is_locked:
                unlock_at = timezone.localtime(existing_user.locked_until).strftime("%Y-%m-%d %H:%M")
                raise ValidationError(
                    f"This account is locked until {unlock_at} after too many failed login attempts."
                )
            if existing_user and not existing_user.is_active:
                if existing_user.email_verified_at is None:
                    raise ValidationError("Please verify your email before logging in.")
                raise ValidationError("This account is inactive. Contact the platform administrator.")

            self.user = authenticate(self.request, username=email.lower(), password=password)
            if self.user is None:
                if existing_user:
                    locked_until = existing_user.register_failed_login()
                    if locked_until:
                        unlock_at = timezone.localtime(locked_until).strftime("%Y-%m-%d %H:%M")
                        raise ValidationError(
                            f"Too many failed login attempts. This account is locked until {unlock_at}."
                        )
                raise ValidationError("Invalid email or password.")
            self.user.clear_login_lock()

        return cleaned_data

    def get_user(self):
        return self.user


class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "phone_number", "profile_image")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "profile_image":
                field.widget.attrs["class"] = "form-control"
            else:
                field.widget.attrs["class"] = "form-control"


class InternalUserCreationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone_number", "role")

    def __init__(self, *args, **kwargs):
        allowed_roles = kwargs.pop(
            "allowed_roles",
            (
                UserRole.AGENT_MANAGER,
                UserRole.DRIVER,
                UserRole.COMPANY_ADMIN,
            ),
        )
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [(role, UserRole(role).label) for role in allowed_roles]
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = user.email.lower()
        user.is_active = True
        user.set_password(self.cleaned_data["password"])
        if user.role == UserRole.SYSTEM_ADMIN:
            user.is_staff = True
            user.is_superuser = True
        if commit:
            user.save()
        return user


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = (
            "name",
            "description",
            "location",
            "address",
            "latitude",
            "longitude",
            "contact_email",
            "contact_phone",
            "efda_license_number",
            "registration_document",
            "logo",
            "admin",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["admin"].queryset = User.objects.filter(role=UserRole.COMPANY_ADMIN)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-control"


class CompanyPremiumSettingsForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = (
            "premium_feature_enabled",
            "premium_streak_threshold",
            "premium_discount_percent",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "premium_feature_enabled":
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class AgentForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = (
            "company",
            "name",
            "description",
            "location_name",
            "address",
            "latitude",
            "longitude",
            "service_radius_km",
            "phone_number",
            "is_active",
            "is_accepting_orders",
            "credit_limit",
            "credit_period_days",
            "admin",
        )

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.fields["admin"].queryset = User.objects.filter(role=UserRole.AGENT_MANAGER)
        if company:
            self.fields["company"].initial = company
            self.fields["company"].queryset = Company.objects.filter(pk=company.pk)
        self.fields["credit_limit"].required = False
        self.fields["credit_period_days"].required = False
        for name, field in self.fields.items():
            if name in {"is_active", "is_accepting_orders"}:
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_credit_limit(self):
        return self.cleaned_data.get("credit_limit") or 0

    def clean_credit_period_days(self):
        return self.cleaned_data.get("credit_period_days") or 14


class DriverForm(forms.ModelForm):
    user = forms.ModelChoiceField(queryset=User.objects.filter(role=UserRole.DRIVER, driver_profile__isnull=True), required=True)

    class Meta:
        model = Driver
        fields = ("agent", "user", "vehicle_identifier", "phone_number", "is_active")

    def __init__(self, *args, **kwargs):
        agent = kwargs.pop("agent", None)
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if agent:
            self.fields["agent"].initial = agent
            self.fields["agent"].queryset = Agent.objects.filter(pk=agent.pk)
        elif company:
            self.fields["agent"].queryset = Agent.objects.filter(company=company)
        for name, field in self.fields.items():
            if name == "is_active":
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class RestockRequestForm(forms.ModelForm):
    class Meta:
        model = RestockRequest
        fields = ("product", "quantity_requested", "note")

    def __init__(self, *args, **kwargs):
        products = kwargs.pop("products", None)
        super().__init__(*args, **kwargs)
        if products is not None:
            self.fields["product"].queryset = products
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class PaymentScheduleForm(forms.ModelForm):
    class Meta:
        model = PaymentSchedule
        fields = ("due_date", "base_price", "excise_tax", "vat", "transport_cost", "amount_paid", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class RestockApprovalForm(forms.Form):
    quantity_approved = forms.IntegerField(min_value=1)
    due_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    base_price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    excise_tax = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, initial=0)
    vat = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, initial=0)
    transport_cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, initial=0)
    amount_paid = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, initial=0)
    status = forms.ChoiceField(choices=PaymentScheduleStatus.choices, initial=PaymentScheduleStatus.PENDING)
    batch_number = forms.CharField(max_length=100)
    expires_at = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    received_at = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class CompanyBatchForm(forms.ModelForm):
    class Meta:
        model = CompanyBatch
        fields = ("product", "batch_number", "production_date", "total_cases_produced", "unit_price", "note")

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
        self.fields["production_date"].widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})
        for name, field in self.fields.items():
            if name != "production_date":
                field.widget.attrs["class"] = "form-control"


class AgentBatchSaleRequestForm(forms.ModelForm):
    class Meta:
        model = AgentBatchSale
        fields = ("batch", "quantity_requested", "payment_type", "requested_upfront_amount", "requested_note")

    def __init__(self, *args, **kwargs):
        agent = kwargs.pop("agent", None)
        super().__init__(*args, **kwargs)
        self.agent = agent
        if agent is not None:
            self.fields["batch"].queryset = CompanyBatch.objects.filter(
                company=agent.company,
                status=CompanyBatchStatus.AVAILABLE,
                unsold_cases_remaining__gt=0,
            ).select_related("product")
        self.fields["requested_upfront_amount"].required = False
        self.fields["requested_note"].required = False
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"batch", "payment_type"} else "form-control"
        self.fields["payment_type"].initial = AgentBatchSalePaymentType.FULL

    def clean_requested_upfront_amount(self):
        return self.cleaned_data.get("requested_upfront_amount") or 0


class AgentBatchSaleApprovalForm(forms.Form):
    quantity_approved = forms.IntegerField(min_value=1)
    unit_price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    initial_payment_amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False)
    credit_due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    decision_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_initial_payment_amount(self):
        return self.cleaned_data.get("initial_payment_amount") or 0


class AgentBatchSalePaymentForm(forms.ModelForm):
    class Meta:
        model = AgentBatchSalePayment
        fields = ("amount", "submitted_note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["submitted_note"].required = False
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class BatchRecallForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"


class DriverLocationForm(forms.ModelForm):
    class Meta:
        model = DriverLocation
        fields = ("latitude", "longitude", "is_online")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "is_online":
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class DriverIssueReportForm(forms.Form):
    issue_type = forms.ChoiceField(choices=DeliveryIssueType.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issue_type"].widget.attrs["class"] = "form-select"
        self.fields["description"].widget.attrs["class"] = "form-control"


class CustomerAddressForm(forms.ModelForm):
    class Meta:
        model = CustomerAddress
        fields = ("label", "address_line", "latitude", "longitude", "notes", "is_default")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "is_default":
                field.widget.attrs["class"] = "form-check-input"
            elif name == "address_line":
                field.widget.attrs["class"] = "form-control"
                field.widget.attrs["rows"] = 3
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_label(self):
        label = (self.cleaned_data.get("label") or "").strip()
        if not self.user or not label:
            return label

        queryset = CustomerAddress.objects.filter(user=self.user, label__iexact=label)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("You already have a saved address with this label.")
        return label


class AgentDriverCreateForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone_number = forms.CharField(max_length=20)
    password = forms.CharField(widget=forms.PasswordInput)
    vehicle_identifier = forms.CharField(max_length=100, required=False)
    is_active = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "is_active" else "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"]
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("A user with this phone number already exists.")
        return phone_number

    @transaction.atomic
    def save(self, agent):
        user = User.objects.create_user(
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password"],
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            phone_number=self.cleaned_data["phone_number"],
            role=UserRole.DRIVER,
            is_active=self.cleaned_data["is_active"],
        )
        return Driver.objects.create(
            agent=agent,
            user=user,
            vehicle_identifier=self.cleaned_data.get("vehicle_identifier", ""),
            phone_number=self.cleaned_data["phone_number"],
            is_active=self.cleaned_data["is_active"],
        )


class AgentDriverUpdateForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone_number = forms.CharField(max_length=20)

    class Meta:
        model = Driver
        fields = ("vehicle_identifier", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = getattr(self.instance, "user", None)
        if user:
            self.fields["first_name"].initial = user.first_name
            self.fields["last_name"].initial = user.last_name
            self.fields["email"].initial = user.email
            self.fields["phone_number"].initial = user.phone_number
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "is_active" else "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        queryset = User.objects.filter(email__iexact=email).exclude(pk=self.instance.user_id)
        if queryset.exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"]
        queryset = User.objects.filter(phone_number=phone_number).exclude(pk=self.instance.user_id)
        if queryset.exists():
            raise ValidationError("A user with this phone number already exists.")
        return phone_number

    @transaction.atomic
    def save(self, commit=True):
        driver = super().save(commit=False)
        user = driver.user
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data["email"]
        user.phone_number = self.cleaned_data["phone_number"]
        user.is_active = self.cleaned_data["is_active"]
        if commit:
            user.save()
            driver.phone_number = self.cleaned_data["phone_number"]
            driver.save()
        return driver


class AgentStockThresholdForm(forms.ModelForm):
    class Meta:
        model = AgentStock
        fields = ("reorder_level",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reorder_level"].widget.attrs["class"] = "form-control"


class AgentInventoryAdjustmentForm(forms.Form):
    CHANGE_DIRECTION_CHOICES = (
        ("increase", "Increase"),
        ("decrease", "Decrease"),
    )

    product = forms.ModelChoiceField(queryset=Product.objects.none())
    transaction_type = forms.ChoiceField(
        choices=(
            (InventoryTransactionType.RESTOCK, "Restock"),
            (InventoryTransactionType.RETURN, "Return"),
            (InventoryTransactionType.ADJUSTMENT, "Adjustment"),
        )
    )
    change_direction = forms.ChoiceField(choices=CHANGE_DIRECTION_CHOICES, initial="increase")
    quantity = forms.IntegerField(min_value=1)
    batch_number = forms.CharField(max_length=100, required=False)
    base_unit_cost = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, initial=0)
    expires_at = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    received_at = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        products = kwargs.pop("products", None)
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = products if products is not None else Product.objects.none()
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        transaction_type = cleaned_data.get("transaction_type")
        change_direction = cleaned_data.get("change_direction")
        quantity = cleaned_data.get("quantity") or 0
        received_at = cleaned_data.get("received_at")
        expires_at = cleaned_data.get("expires_at")

        if transaction_type == InventoryTransactionType.RETURN and change_direction != "increase":
            raise ValidationError("Returns should add stock back into the warehouse.")
        if transaction_type == InventoryTransactionType.RESTOCK and change_direction != "increase":
            raise ValidationError("Restocks should increase stock.")

        if change_direction == "increase":
            cleaned_data["received_at"] = received_at or timezone.localdate()
            cleaned_data["expires_at"] = expires_at or (cleaned_data["received_at"] + timezone.timedelta(days=365))
            cleaned_data["quantity_change"] = quantity
        else:
            cleaned_data["quantity_change"] = -quantity
        return cleaned_data


class CompanyProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("name", "size_label", "description", "price", "available_quantity", "image", "is_active")

    def __init__(self, *args, **kwargs):
        self.company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "is_active" else "form-control"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not self.company or not name:
            return name
        queryset = Product.objects.filter(company=self.company, name__iexact=name)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A product with this name already exists for your company.")
        return name


class CompanyAgentUpdateForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = (
            "name",
            "description",
            "location_name",
            "address",
            "latitude",
            "longitude",
            "service_radius_km",
            "phone_number",
            "is_active",
            "is_accepting_orders",
            "credit_limit",
            "credit_period_days",
            "admin",
        )

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        queryset = User.objects.filter(role=UserRole.AGENT_MANAGER)
        if company is not None:
            queryset = queryset.filter(models.Q(managed_agent_branches__company=company) | models.Q(managed_agent_branches__isnull=True)).distinct()
        self.fields["admin"].queryset = queryset
        self.fields["credit_limit"].required = False
        self.fields["credit_period_days"].required = False
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name in {"is_active", "is_accepting_orders"} else "form-control"

    def clean_credit_limit(self):
        return self.cleaned_data.get("credit_limit") or self.instance.credit_limit or 0

    def clean_credit_period_days(self):
        return self.cleaned_data.get("credit_period_days") or self.instance.credit_period_days or 14


class SystemUserUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone_number", "role", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [(role.value, role.label) for role in UserRole]
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "is_active" else "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        queryset = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"]
        queryset = User.objects.filter(phone_number=phone_number).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A user with this phone number already exists.")
        return phone_number

    def save(self, commit=True):
        user = super().save(commit=False)
        if user.role == UserRole.SYSTEM_ADMIN:
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False
        if commit:
            user.save()
        return user


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ("title", "message", "target_role")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_role"].choices = [(choice.value, choice.label) for choice in AnnouncementTargetRole]
        for name, field in self.fields.items():
            if name == "message":
                field.widget.attrs["class"] = "form-control"
                field.widget.attrs["rows"] = 4
            else:
                field.widget.attrs["class"] = "form-control"
