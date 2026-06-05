from django import forms
from django.core.exceptions import ValidationError

from orders.models import LocationSource


class CheckoutForm(forms.Form):
    location_source = forms.ChoiceField(
        choices=LocationSource.choices,
        widget=forms.RadioSelect,
        initial=LocationSource.CURRENT,
    )
    selected_agent_id = forms.CharField(required=False, widget=forms.HiddenInput)
    delivery_address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    latitude = forms.DecimalField(widget=forms.HiddenInput)
    longitude = forms.DecimalField(widget=forms.HiddenInput)
    phone_number = forms.CharField(max_length=20)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"
