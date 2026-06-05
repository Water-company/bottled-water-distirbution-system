from django import forms


class AddToCartForm(forms.Form):
    quantity = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].widget.attrs["class"] = "form-control"


class UpdateCartItemForm(forms.Form):
    quantity = forms.IntegerField(min_value=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].widget.attrs["class"] = "form-control"
