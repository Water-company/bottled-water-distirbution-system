from django import forms
from django.core.exceptions import ValidationError

from accounts.models import CustomerAddress
from accounts.validators import normalize_ethiopian_phone_number, validate_file_size
from orders.models import ComplaintCategory, LocationSource, RefundPayoutMethod


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


EVIDENCE_MAX_SIZE_VALIDATOR = validate_file_size(5)
ALLOWED_COMPLAINT_EVIDENCE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
}


class CheckoutForm(forms.Form):
    location_source = forms.ChoiceField(
        choices=LocationSource.choices,
        widget=forms.RadioSelect,
        initial=LocationSource.CURRENT,
    )
    saved_address_id = forms.CharField(required=False, widget=forms.HiddenInput)
    selected_agent_id = forms.CharField(required=False, widget=forms.HiddenInput)
    delivery_address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    latitude = forms.DecimalField(widget=forms.HiddenInput)
    longitude = forms.DecimalField(widget=forms.HiddenInput)
    phone_number = forms.CharField(max_length=20)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "location_source":
                field.widget.attrs["class"] = "form-check-input"
            elif name not in {"selected_agent_id", "latitude", "longitude"}:
                field.widget.attrs["class"] = "form-control"
        self.fields["phone_number"].widget.attrs["placeholder"] = "+2519XXXXXXXX"

    def clean_phone_number(self):
        return normalize_ethiopian_phone_number(self.cleaned_data["phone_number"], required=True)

    def clean(self):
        cleaned_data = super().clean()
        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        selected_agent_id = (cleaned_data.get("selected_agent_id") or "").strip()
        saved_address_id = (cleaned_data.get("saved_address_id") or "").strip()
        saved_address = None
        if saved_address_id:
            try:
                saved_address = CustomerAddress.objects.get(pk=saved_address_id, user=self.user)
            except (CustomerAddress.DoesNotExist, ValueError):
                raise ValidationError("Please choose one of your saved addresses or pin a valid delivery point.")
            if latitude is None:
                cleaned_data["latitude"] = saved_address.latitude
            if longitude is None:
                cleaned_data["longitude"] = saved_address.longitude
            if not (cleaned_data.get("delivery_address") or "").strip():
                cleaned_data["delivery_address"] = saved_address.address_line
            if not (cleaned_data.get("notes") or "").strip() and saved_address.notes:
                cleaned_data["notes"] = saved_address.notes

        latitude = cleaned_data.get("latitude")
        longitude = cleaned_data.get("longitude")
        if latitude is None or longitude is None:
            raise ValidationError("Please choose a delivery location before submitting your order request.")
        if not (-90 <= float(latitude) <= 90):
            raise ValidationError("Please choose a valid map latitude for delivery.")
        if not (-180 <= float(longitude) <= 180):
            raise ValidationError("Please choose a valid map longitude for delivery.")
        if not selected_agent_id:
            raise ValidationError("Please choose one of the nearby eligible agents before sending the request.")
        delivery_address = (cleaned_data.get("delivery_address") or "").strip()
        if not delivery_address:
            cleaned_data["delivery_address"] = f"Pinned location ({latitude}, {longitude})"
        cleaned_data["notes"] = (cleaned_data.get("notes") or "").strip()
        return cleaned_data


class RefundRequestForm(forms.Form):
    category = forms.ChoiceField(choices=ComplaintCategory.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), min_length=10)
    evidence_files = forms.FileField(
        required=False,
        widget=MultipleFileInput(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].widget.attrs["class"] = "form-select"
        self.fields["description"].widget.attrs["class"] = "form-control"
        self.fields["evidence_files"].widget.attrs["class"] = "form-control"
        self.fields["evidence_files"].help_text = "Optional: upload up to 3 images or PDF documents."

    def clean_evidence_files(self):
        files = self.files.getlist("evidence_files")
        if len(files) > 3:
            raise ValidationError("You can upload up to 3 complaint evidence files.")
        for item in files:
            content_type = getattr(item, "content_type", "") or ""
            if content_type not in ALLOWED_COMPLAINT_EVIDENCE_TYPES:
                raise ValidationError("Complaint evidence must be JPG, PNG, GIF, WEBP, or PDF files.")
            EVIDENCE_MAX_SIZE_VALIDATOR(item)
        return files


class DeliveryFeedbackForm(forms.Form):
    rating = forms.IntegerField(min_value=1, max_value=5)
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    photo = forms.ImageField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rating"].widget.attrs["class"] = "form-control"
        self.fields["comment"].widget.attrs["class"] = "form-control"
        self.fields["photo"].widget.attrs["class"] = "form-control"

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo:
            EVIDENCE_MAX_SIZE_VALIDATOR(photo)
        return photo
