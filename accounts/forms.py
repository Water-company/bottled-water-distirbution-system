from django import forms
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError

from accounts.models import User, UserRole
from catalog.models import Agent, Company, Driver, PaymentSchedule, PaymentScheduleStatus, RestockRequest
from core.models import DriverLocation


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
        email = cleaned_data.get("email")
        password = cleaned_data.get("password")

        if email and password:
            self.user = authenticate(self.request, username=email.lower(), password=password)
            if self.user is None:
                raise ValidationError("Invalid email or password.")

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
            "admin",
        )

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.fields["admin"].queryset = User.objects.filter(role=UserRole.AGENT_MANAGER)
        if company:
            self.fields["company"].initial = company
            self.fields["company"].queryset = Company.objects.filter(pk=company.pk)
        for name, field in self.fields.items():
            if name in {"is_active", "is_accepting_orders"}:
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


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
