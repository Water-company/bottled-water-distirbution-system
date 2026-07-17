from django import forms
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomerAddress, User, UserRole
from accounts.validators import (
    normalize_ethiopian_phone_number,
    validate_document_content_type,
    validate_file_size,
)
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


def normalize_required_phone(value):
    return normalize_ethiopian_phone_number(value, required=True)


def normalize_optional_phone(value):
    return normalize_ethiopian_phone_number(value, required=False)


DOCUMENT_MAX_SIZE_VALIDATOR = validate_file_size(10)
IMAGE_MAX_SIZE_VALIDATOR = validate_file_size(5)
REGISTRATION_DOCUMENT_CONTENT_TYPE_VALIDATOR = validate_document_content_type(
    ("application/pdf", "image/jpeg", "image/png")
)


def run_upload_validators(upload, *validators):
    if not upload:
        return upload
    for validator in validators:
        validator(upload)
    return upload


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
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
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

    def clean_phone_number(self):
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
        queryset = User.objects.filter(phone_number=phone_number).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("An account with this phone number already exists.")
        return phone_number

    def clean_profile_image(self):
        return run_upload_validators(
            self.cleaned_data.get("profile_image"),
            IMAGE_MAX_SIZE_VALIDATOR,
        )


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

    def clean_phone_number(self):
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("An account with this phone number already exists.")
        return phone_number

    def clean_password(self):
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password

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
        admin_queryset = User.objects.filter(role=UserRole.COMPANY_ADMIN)
        if self.instance.pk:
            admin_queryset = admin_queryset.filter(managed_company=self.instance)
        else:
            admin_queryset = admin_queryset.none()
        self.fields["admin"].queryset = admin_queryset
        self.fields["admin"].help_text = "Primary company admins are created during company onboarding."
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-control"

    def clean_contact_phone(self):
        return normalize_optional_phone(self.cleaned_data.get("contact_phone"))

    def clean_registration_document(self):
        return run_upload_validators(
            self.cleaned_data.get("registration_document"),
            DOCUMENT_MAX_SIZE_VALIDATOR,
            REGISTRATION_DOCUMENT_CONTENT_TYPE_VALIDATOR,
        )

    def clean_logo(self):
        return run_upload_validators(
            self.cleaned_data.get("logo"),
            IMAGE_MAX_SIZE_VALIDATOR,
        )


class SystemCompanyRegistrationForm(forms.ModelForm):
    premium_feature_enabled = forms.TypedChoiceField(
        label="Enable Premium Customer Program?",
        choices=(("true", "Yes"), ("false", "No")),
        coerce=lambda value: str(value).lower() == "true",
        empty_value=False,
        widget=forms.RadioSelect,
        initial="false",
    )
    admin_first_name = forms.CharField(max_length=150)
    admin_last_name = forms.CharField(max_length=150)
    admin_email = forms.EmailField()
    admin_phone_number = forms.CharField(max_length=20)
    admin_password = forms.CharField(widget=forms.PasswordInput)

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
            "premium_feature_enabled",
            "premium_streak_threshold",
            "premium_discount_percent",
            "registration_document",
            "logo",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["premium_streak_threshold"].required = False
        self.fields["premium_discount_percent"].required = False
        self.fields["premium_streak_threshold"].label = "Consecutive Purchases Required"
        self.fields["premium_discount_percent"].label = "Discount Percentage"
        self.fields["premium_streak_threshold"].widget.attrs.update(
            {
                "class": "form-control",
                "min": "1",
                "step": "1",
                "placeholder": "5",
            }
        )
        self.fields["premium_discount_percent"].widget.attrs.update(
            {
                "class": "form-control",
                "min": "1",
                "max": "100",
                "step": "0.01",
                "placeholder": "15",
            }
        )
        self.fields["premium_feature_enabled"].widget.attrs["class"] = "d-flex gap-3"
        for name, field in self.fields.items():
            if name not in {"premium_feature_enabled", "premium_streak_threshold", "premium_discount_percent"}:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        premium_enabled = cleaned_data.get("premium_feature_enabled")
        threshold = cleaned_data.get("premium_streak_threshold")
        discount_percent = cleaned_data.get("premium_discount_percent")

        if premium_enabled:
            if threshold in {None, ""}:
                self.add_error("premium_streak_threshold", "Enter the consecutive purchases required.")
            elif threshold < 1:
                self.add_error("premium_streak_threshold", "Consecutive purchases required must be greater than zero.")

            if discount_percent in {None, ""}:
                self.add_error("premium_discount_percent", "Enter the discount percentage.")
            elif discount_percent < 1 or discount_percent > 100:
                self.add_error("premium_discount_percent", "Discount percentage must be between 1 and 100.")
        else:
            cleaned_data["premium_streak_threshold"] = (
                self.instance.premium_streak_threshold
                if self.instance.pk
                else Company._meta.get_field("premium_streak_threshold").get_default()
            )
            cleaned_data["premium_discount_percent"] = (
                self.instance.premium_discount_percent
                if self.instance.pk
                else Company._meta.get_field("premium_discount_percent").get_default()
            )

        return cleaned_data

    def clean_admin_email(self):
        email = self.cleaned_data["admin_email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this company admin email already exists.")
        return email

    def clean_contact_phone(self):
        return normalize_optional_phone(self.cleaned_data.get("contact_phone"))

    def clean_registration_document(self):
        return run_upload_validators(
            self.cleaned_data.get("registration_document"),
            DOCUMENT_MAX_SIZE_VALIDATOR,
            REGISTRATION_DOCUMENT_CONTENT_TYPE_VALIDATOR,
        )

    def clean_logo(self):
        return run_upload_validators(
            self.cleaned_data.get("logo"),
            IMAGE_MAX_SIZE_VALIDATOR,
        )

    def clean_admin_phone_number(self):
        phone_number = normalize_required_phone(self.cleaned_data["admin_phone_number"])
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("A user with this company admin phone number already exists.")
        return phone_number

    def clean_admin_password(self):
        password = self.cleaned_data["admin_password"]
        password_validation.validate_password(password)
        return password

    @transaction.atomic
    def save(self, commit=True):
        company = super().save(commit=False)
        if not commit:
            return company

        company.save()
        admin_user = User.objects.create_user(
            email=self.cleaned_data["admin_email"],
            password=self.cleaned_data["admin_password"],
            first_name=self.cleaned_data["admin_first_name"],
            last_name=self.cleaned_data["admin_last_name"],
            phone_number=self.cleaned_data["admin_phone_number"],
            role=UserRole.COMPANY_ADMIN,
            managed_company=company,
            is_active=False,
        )
        admin_user.email_verified_at = None
        admin_user.save(update_fields=["email_verified_at", "updated_at"])
        company.admin = admin_user
        company.save(update_fields=["admin", "updated_at"])
        return company


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

    def clean(self):
        cleaned_data = super().clean()
        premium_enabled = cleaned_data.get("premium_feature_enabled")
        threshold = cleaned_data.get("premium_streak_threshold")
        discount_percent = cleaned_data.get("premium_discount_percent")

        if premium_enabled:
            if threshold in {None, ""}:
                self.add_error("premium_streak_threshold", "Enter the consecutive purchases required.")
            elif threshold < 1:
                self.add_error("premium_streak_threshold", "Consecutive purchases required must be greater than zero.")

            if discount_percent in {None, ""}:
                self.add_error("premium_discount_percent", "Enter the discount percentage.")
            elif discount_percent < 1 or discount_percent > 100:
                self.add_error("premium_discount_percent", "Discount percentage must be between 1 and 100.")
        else:
            cleaned_data["premium_streak_threshold"] = self.instance.premium_streak_threshold
            cleaned_data["premium_discount_percent"] = self.instance.premium_discount_percent

        return cleaned_data


class CompanyCreditPolicyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ("allow_agent_credit", "maximum_credit_duration_days")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allow_agent_credit"].label = "Allow Agent Credit?"
        self.fields["allow_agent_credit"].required = False
        self.fields["maximum_credit_duration_days"].label = "Maximum Credit Duration"
        self.fields["maximum_credit_duration_days"].required = False
        self.fields["maximum_credit_duration_days"].widget.attrs.update(
            {
                "class": "form-control",
                "min": 1,
                "step": 1,
                "placeholder": "e.g. 14",
            }
        )
        self.fields["allow_agent_credit"].widget.attrs["class"] = "form-check-input"

    def clean(self):
        cleaned_data = super().clean()
        allow_agent_credit = cleaned_data.get("allow_agent_credit")
        maximum_credit_duration_days = cleaned_data.get("maximum_credit_duration_days")

        if allow_agent_credit:
            if maximum_credit_duration_days in {None, ""}:
                self.add_error("maximum_credit_duration_days", "Enter the maximum credit duration in days.")
            elif maximum_credit_duration_days < 1:
                self.add_error("maximum_credit_duration_days", "Maximum credit duration must be a positive integer.")
        else:
            cleaned_data["maximum_credit_duration_days"] = (
                self.instance.maximum_credit_duration_days if self.instance.pk else 14
            )

        return cleaned_data


class AgentForm(forms.Form):
    manager_first_name = forms.CharField(max_length=150)
    manager_last_name = forms.CharField(max_length=150)
    manager_email = forms.EmailField()
    manager_phone_number = forms.CharField(max_length=20)
    manager_password1 = forms.CharField(
        label="Manager Password",
        strip=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=password_validation.password_validators_help_text_html(),
    )
    manager_password2 = forms.CharField(
        label="Confirm Manager Password",
        strip=False,
        widget=forms.PasswordInput(render_value=False),
    )
    name = forms.CharField(max_length=255)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    location_name = forms.CharField(max_length=255)
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    latitude = forms.DecimalField(max_digits=9, decimal_places=6)
    longitude = forms.DecimalField(max_digits=9, decimal_places=6)
    service_radius_km = forms.DecimalField(max_digits=6, decimal_places=2, initial=15)
    phone_number = forms.CharField(max_length=20, required=False)
    is_active = forms.BooleanField(required=False, initial=True)
    is_accepting_orders = forms.BooleanField(required=False, initial=True)
    credit_limit = forms.DecimalField(max_digits=12, decimal_places=2, required=False, initial=0)
    credit_period_days = forms.IntegerField(required=False, min_value=1, initial=14)

    def __init__(self, *args, **kwargs):
        self.company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if self.company is None:
            raise ValueError("AgentForm requires a company.")

        self.fields["manager_first_name"].label = "Manager First Name"
        self.fields["manager_last_name"].label = "Manager Last Name"
        self.fields["manager_email"].label = "Manager Email"
        self.fields["manager_phone_number"].label = "Manager Phone Number"
        self.fields["manager_phone_number"].help_text = "This must be a brand-new account and cannot belong to another company."
        self.fields["name"].label = "Agent Branch Name"
        self.fields["phone_number"].label = "Branch Phone Number"

        for name, field in self.fields.items():
            if name in {"is_active", "is_accepting_orders"}:
                field.widget.attrs["class"] = "form-check-input"
            else:
                existing_class = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"{existing_class} form-control".strip()

    def clean_manager_email(self):
        email = (self.cleaned_data.get("manager_email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def clean_manager_phone_number(self):
        phone_number = normalize_required_phone(self.cleaned_data.get("manager_phone_number"))
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("An account with this phone number already exists.")
        return phone_number

    def clean_phone_number(self):
        return normalize_optional_phone(self.cleaned_data.get("phone_number"))

    def clean_credit_limit(self):
        return self.cleaned_data.get("credit_limit") or 0

    def clean_credit_period_days(self):
        return self.cleaned_data.get("credit_period_days") or 14

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("manager_password1")
        password2 = cleaned_data.get("manager_password2")

        if password1 and password2 and password1 != password2:
            self.add_error("manager_password2", "The two password fields must match.")

        if password1:
            candidate_user = User(
                email=cleaned_data.get("manager_email"),
                first_name=cleaned_data.get("manager_first_name"),
                last_name=cleaned_data.get("manager_last_name"),
                phone_number=cleaned_data.get("manager_phone_number"),
                role=UserRole.AGENT_MANAGER,
            )
            try:
                password_validation.validate_password(password1, user=candidate_user)
            except ValidationError as exc:
                self.add_error("manager_password1", exc)

        return cleaned_data

    @transaction.atomic
    def save(self):
        manager_user = User.objects.create_user(
            email=self.cleaned_data["manager_email"],
            password=self.cleaned_data["manager_password1"],
            first_name=self.cleaned_data["manager_first_name"],
            last_name=self.cleaned_data["manager_last_name"],
            phone_number=self.cleaned_data["manager_phone_number"],
            role=UserRole.AGENT_MANAGER,
            is_active=True,
        )
        return Agent.objects.create(
            company=self.company,
            name=self.cleaned_data["name"],
            description=self.cleaned_data["description"],
            location_name=self.cleaned_data["location_name"],
            address=self.cleaned_data["address"],
            latitude=self.cleaned_data["latitude"],
            longitude=self.cleaned_data["longitude"],
            service_radius_km=self.cleaned_data["service_radius_km"],
            phone_number=self.cleaned_data["phone_number"],
            is_active=self.cleaned_data["is_active"],
            is_accepting_orders=self.cleaned_data["is_accepting_orders"],
            credit_limit=self.cleaned_data["credit_limit"],
            credit_period_days=self.cleaned_data["credit_period_days"],
            admin=manager_user,
        )


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

    def clean_phone_number(self):
        return normalize_optional_phone(self.cleaned_data.get("phone_number"))


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

    def clean(self):
        cleaned_data = super().clean()
        due_date = cleaned_data.get("due_date")
        expires_at = cleaned_data.get("expires_at")
        received_at = cleaned_data.get("received_at")
        today = timezone.localdate()
        if due_date and due_date < today:
            raise ValidationError("Due date cannot be in the past.")
        if received_at and received_at > today:
            raise ValidationError("Received date cannot be in the future.")
        if expires_at and received_at and expires_at < received_at:
            raise ValidationError("Expiry date must be on or after the received date.")
        return cleaned_data


class CompanyBatchForm(forms.ModelForm):
    class Meta:
        model = CompanyBatch
        fields = ("product", "batch_number", "production_date", "total_cases_produced", "unit_price", "note")

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
        self.fields["product"].help_text = "Only active company products can be produced into a batch."
        self.fields["batch_number"].help_text = "Use a unique identifier like BATCH-2026-001."
        self.fields["production_date"].help_text = "Production date cannot be in the future."
        self.fields["total_cases_produced"].help_text = "Enter the number of cases produced in this run."
        self.fields["unit_price"].help_text = "This becomes the default price per case when agents request from this batch."
        self.fields["production_date"].widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})
        for name, field in self.fields.items():
            if name != "production_date":
                field.widget.attrs["class"] = "form-control"

    def clean_production_date(self):
        production_date = self.cleaned_data["production_date"]
        if production_date > timezone.localdate():
            raise ValidationError("Production date cannot be in the future.")
        return production_date


class AgentBatchSaleRequestForm(forms.ModelForm):
    class Meta:
        model = AgentBatchSale
        fields = ("batch", "quantity_requested", "payment_type", "requested_upfront_amount", "requested_note")

    def __init__(self, *args, **kwargs):
        agent = kwargs.pop("agent", None)
        super().__init__(*args, **kwargs)
        self.agent = agent
        self.fields["batch"].label = "Choose product batch"
        self.fields["quantity_requested"].label = "Cases needed"
        self.fields["payment_type"].label = "Payment type"
        self.fields["requested_upfront_amount"].label = "Amount paying now (ETB)"
        self.fields["requested_note"].label = "Note to company admin"
        self.fields["batch"].help_text = "Pick the exact company batch you want to request. Each option shows the product, remaining cases, and unit price."
        self.fields["quantity_requested"].help_text = "Enter the number of cases you need for this branch."
        self.fields["payment_type"].help_text = "Choose whether this stock is fully paid, partially paid, or taken on credit."
        self.fields["requested_upfront_amount"].help_text = "For full payment this can be left blank and the full amount will be assumed automatically."
        self.fields["requested_note"].help_text = "Optional context like urgency, route needs, or warehouse notes."
        if agent is not None:
            self.fields["batch"].queryset = CompanyBatch.objects.filter(
                company=agent.company,
                status=CompanyBatchStatus.AVAILABLE,
                unsold_cases_remaining__gt=0,
            ).select_related("product")
        self.fields["batch"].empty_label = "Select an available company batch"
        self.fields["requested_upfront_amount"].required = False
        self.fields["requested_note"].required = False
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"batch", "payment_type"} else "form-control"
        self.fields["quantity_requested"].widget.attrs.update({"min": "1", "step": "1", "placeholder": "e.g. 40"})
        self.fields["requested_upfront_amount"].widget.attrs.update({"min": "0", "step": "0.01", "placeholder": "0.00"})
        self.fields["requested_note"].widget.attrs["rows"] = 3
        self.fields["payment_type"].initial = AgentBatchSalePaymentType.FULL
        self.fields["batch"].label_from_instance = self._label_batch_option

    def clean_requested_upfront_amount(self):
        return self.cleaned_data.get("requested_upfront_amount") or 0

    @staticmethod
    def _label_batch_option(batch):
        return (
            f"{batch.product.name} ({batch.product.size_label or 'Size not set'}) | "
            f"{batch.batch_number} | {batch.unsold_cases_remaining} cases left | ETB {batch.unit_price}/case"
        )


class AgentInventoryPurchaseForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=CompanyBatch.objects.none())
    quantity_requested = forms.IntegerField(min_value=1)
    payment_type = forms.ChoiceField(
        choices=(
            (AgentBatchSalePaymentType.FULL, "Pay now with Chapa"),
            (AgentBatchSalePaymentType.CREDIT, "Take on credit"),
        )
    )
    requested_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        agent = kwargs.pop("agent", None)
        super().__init__(*args, **kwargs)
        self.fields["batch"].label = "Choose product batch"
        self.fields["quantity_requested"].label = "Cases to buy"
        self.fields["payment_type"].label = "How to pay"
        self.fields["requested_note"].label = "Optional note"
        self.fields["batch"].help_text = "Pick the exact company batch you want to restock from right now."
        self.fields["quantity_requested"].help_text = "Enter the number of cases you want to move into branch inventory."
        self.fields["payment_type"].help_text = "Pay now to continue to Chapa, or take the stock on credit if your company policy allows it."
        self.fields["requested_note"].help_text = "Optional context for the company admin and branch audit trail."
        if agent is not None:
            self.fields["batch"].queryset = CompanyBatch.objects.filter(
                company=agent.company,
                status=CompanyBatchStatus.AVAILABLE,
                unsold_cases_remaining__gt=0,
            ).select_related("product")
        self.fields["batch"].empty_label = "Select an available company batch"
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"batch", "payment_type"} else "form-control"
        self.fields["quantity_requested"].widget.attrs.update({"min": "1", "step": "1", "placeholder": "e.g. 40"})
        self.fields["payment_type"].initial = AgentBatchSalePaymentType.FULL
        self.fields["batch"].label_from_instance = AgentBatchSaleRequestForm._label_batch_option


class AgentBatchSaleApprovalForm(forms.Form):
    quantity_approved = forms.IntegerField(min_value=1)
    unit_price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    initial_payment_amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False)
    credit_terms_days = forms.IntegerField(
        min_value=1,
        required=False,
        label="Days to repay after stock is confirmed received.",
    )
    decision_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["credit_terms_days"].widget.attrs.update({"min": "1", "step": "1", "placeholder": "e.g. 14"})
        self.fields["credit_terms_days"].help_text = (
            "Use this for partial-payment or credit approvals. The countdown starts when the agent confirms receipt."
        )

    def clean_initial_payment_amount(self):
        return self.cleaned_data.get("initial_payment_amount") or 0

    def clean(self):
        cleaned_data = super().clean()
        credit_terms_days = cleaned_data.get("credit_terms_days")
        if credit_terms_days is not None and credit_terms_days < 1:
            raise ValidationError("Credit terms must be at least one day.")
        return cleaned_data


class AgentBatchSaleReceiptForm(forms.Form):
    receipt_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["receipt_note"].widget.attrs["class"] = "form-control"
        self.fields["receipt_note"].label = "Receipt note"
        self.fields["receipt_note"].help_text = "Optional warehouse or handoff note for the confirmed receipt."


class AgentBatchSaleCancellationForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"
        self.fields["reason"].label = "Cancellation reason"


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

    def clean(self):
        cleaned_data = super().clean()
        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        if latitude is not None and not (-90 <= float(latitude) <= 90):
            raise ValidationError("Driver latitude must be between -90 and 90.")
        if longitude is not None and not (-180 <= float(longitude) <= 180):
            raise ValidationError("Driver longitude must be between -180 and 180.")
        return cleaned_data


class OrderTrackingUpdateForm(forms.Form):
    order_id = forms.IntegerField(min_value=1)
    driver_id = forms.IntegerField(min_value=1)
    latitude = forms.DecimalField(max_digits=9, decimal_places=6)
    longitude = forms.DecimalField(max_digits=9, decimal_places=6)
    recorded_at = forms.DateTimeField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        if latitude is not None and not (-90 <= float(latitude) <= 90):
            raise ValidationError("Driver latitude must be between -90 and 90.")
        if longitude is not None and not (-180 <= float(longitude) <= 180):
            raise ValidationError("Driver longitude must be between -180 and 180.")
        return cleaned_data


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

    def clean(self):
        cleaned_data = super().clean()
        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        if latitude is not None and not (-90 <= float(latitude) <= 90):
            raise ValidationError("Address latitude must be between -90 and 90.")
        if longitude is not None and not (-180 <= float(longitude) <= 180):
            raise ValidationError("Address longitude must be between -180 and 180.")
        return cleaned_data


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
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
        if User.objects.filter(phone_number=phone_number).exists():
            raise ValidationError("A user with this phone number already exists.")
        return phone_number

    def clean_password(self):
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password

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
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
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
        self.fields["size_label"].help_text = "Examples: 0.5L, 5L, 20L"
        self.fields["price"].help_text = "Selling price per case or unit used across orders and internal stock setup."
        self.fields["available_quantity"].help_text = "Starting public catalog quantity. Agent branch stock is tracked separately."
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

    def clean_image(self):
        return run_upload_validators(
            self.cleaned_data.get("image"),
            IMAGE_MAX_SIZE_VALIDATOR,
        )


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

    def clean_phone_number(self):
        return normalize_optional_phone(self.cleaned_data.get("phone_number"))


class SystemUserUpdateForm(forms.ModelForm):
    managed_company = forms.ModelChoiceField(
        queryset=Company.objects.order_by("name"),
        required=False,
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone_number", "role", "managed_company", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [(role.value, role.label) for role in UserRole]
        self.fields["managed_company"].label = "Managed company"
        self.fields["managed_company"].help_text = "Only company admin users should be attached to a company."
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-check-input" if name == "is_active" else "form-control"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        queryset = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_phone_number(self):
        phone_number = normalize_required_phone(self.cleaned_data["phone_number"])
        queryset = User.objects.filter(phone_number=phone_number).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A user with this phone number already exists.")
        return phone_number

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")
        managed_company = cleaned_data.get("managed_company")
        try:
            primary_company = self.instance.primary_managed_company
        except Company.DoesNotExist:
            primary_company = None

        if role == UserRole.COMPANY_ADMIN and managed_company is None:
            self.add_error("managed_company", "Company admins must be assigned to exactly one company.")
        if primary_company is not None:
            if role != UserRole.COMPANY_ADMIN:
                self.add_error("role", "Reassign this company's primary admin before changing the user's role.")
            elif managed_company is not None and managed_company.pk != primary_company.pk:
                self.add_error("managed_company", "Primary company admins must stay assigned to their company.")
        if role != UserRole.COMPANY_ADMIN:
            cleaned_data["managed_company"] = None
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        if user.role == UserRole.SYSTEM_ADMIN:
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False
        user.managed_company = self.cleaned_data.get("managed_company")
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
