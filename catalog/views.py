from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect
from django.views.generic import DetailView, ListView

from cart.forms import AddToCartForm
from catalog.forms import CompanyFilterForm, ProductFilterForm
from catalog.models import Company, Product


class CompanyListView(ListView):
    model = Company
    template_name = "catalog/company_list.html"
    context_object_name = "companies"
    paginate_by = 8

    def get_queryset(self):
        queryset = Company.objects.filter(is_verified=True, is_active=True)
        self.filter_form = CompanyFilterForm(self.request.GET or None)
        if self.filter_form.is_valid():
            search = self.filter_form.cleaned_data.get("search")
            location = self.filter_form.cleaned_data.get("location")
            if search:
                queryset = queryset.filter(name__icontains=search)
            if location:
                queryset = queryset.filter(location__icontains=location)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["query_string"] = query_params.urlencode()
        return context


class ProductListView(ListView):
    model = Product
    template_name = "catalog/product_list.html"
    context_object_name = "products"
    paginate_by = 9

    def get_queryset(self):
        queryset = Product.objects.select_related("company").filter(
            is_active=True,
            company__is_verified=True,
            company__is_active=True,
        )
        self.filter_form = ProductFilterForm(self.request.GET or None)
        if self.filter_form.is_valid():
            search = self.filter_form.cleaned_data.get("search")
            company = self.filter_form.cleaned_data.get("company")
            min_price = self.filter_form.cleaned_data.get("min_price")
            max_price = self.filter_form.cleaned_data.get("max_price")
            sort = self.filter_form.cleaned_data.get("sort") or "newest"

            if search:
                queryset = queryset.filter(
                    Q(name__icontains=search)
                    | Q(description__icontains=search)
                    | Q(company__name__icontains=search)
                )
            if company:
                queryset = queryset.filter(company=company)
            if min_price is not None:
                queryset = queryset.filter(price__gte=min_price)
            if max_price is not None:
                queryset = queryset.filter(price__lte=max_price)
            if sort == "price_asc":
                queryset = queryset.order_by("price", "-created_at")
            elif sort == "price_desc":
                queryset = queryset.order_by("-price", "-created_at")
            else:
                queryset = queryset.order_by("-created_at")
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["query_string"] = query_params.urlencode()
        return context


class ProductDetailView(DetailView):
    model = Product
    template_name = "catalog/product_detail.html"
    context_object_name = "product"

    def get_queryset(self):
        return Product.objects.select_related("company").prefetch_related("gallery").filter(
            is_active=True,
            company__is_verified=True,
            company__is_active=True,
        )

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.info(request, "Please log in to view detailed products and place an order.")
            return redirect(f"/accounts/login/?next={request.path}")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["add_to_cart_form"] = AddToCartForm()
        context["related_products"] = (
            Product.objects.filter(company=self.object.company, is_active=True, company__is_active=True)
            .exclude(pk=self.object.pk)[:4]
        )
        return context
