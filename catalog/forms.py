from django import forms

from catalog.models import Company


class CompanyFilterForm(forms.Form):
    search = forms.CharField(required=False)
    location = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ProductFilterForm(forms.Form):
    search = forms.CharField(required=False)
    company = forms.ModelChoiceField(queryset=Company.objects.none(), required=False)
    min_price = forms.DecimalField(required=False, min_value=0)
    max_price = forms.DecimalField(required=False, min_value=0)
    sort = forms.ChoiceField(
        required=False,
        choices=(
            ("newest", "Newest"),
            ("price_asc", "Price Low to High"),
            ("price_desc", "Price High to Low"),
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.filter(is_verified=True).order_by("name")
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name in {"company", "sort"} else "form-control"
