from django import forms
from django.core.exceptions import ValidationError

from accounts.models import CustomerAddress
from orders.models import LocationSource, RefundPayoutMethod


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


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
        if not selected_agent_id:
            raise ValidationError("Please choose one of the nearby eligible agents before continuing to payment.")
        delivery_address = (cleaned_data.get("delivery_address") or "").strip()
        if not delivery_address:
            cleaned_data["delivery_address"] = f"Pinned location ({latitude}, {longitude})"
        return cleaned_data


class RefundRequestForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), min_length=10)
    payout_method = forms.ChoiceField(choices=RefundPayoutMethod.choices, initial=RefundPayoutMethod.GATEWAY)
    photos = forms.FileField(
        required=False,
        widget=MultipleFileInput(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"
        self.fields["payout_method"].widget.attrs["class"] = "form-select"
        self.fields["photos"].widget.attrs["class"] = "form-control"

    def clean_photos(self):
        files = self.files.getlist("photos")
        if len(files) > 3:
            raise ValidationError("You can upload up to 3 refund photos.")
        for item in files:
            content_type = getattr(item, "content_type", "") or ""
            if not content_type.startswith("image/"):
                raise ValidationError("Refund evidence must be image files.")
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
