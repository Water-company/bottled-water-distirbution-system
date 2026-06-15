from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views import View
from django.views.generic import ListView

from catalog.forms import CompanyFilterForm
from catalog.models import Company
from core.map_services import reverse_geocode_coordinate, search_addis_locations


class HomeLandingView(ListView):
    model = Company
    template_name = "home.html"
    context_object_name = "companies"
    paginate_by = 6

    def get_queryset(self):
        queryset = (
            Company.objects.filter(
                is_verified=True,
                is_active=True,
                products__is_active=True,
                products__available_quantity__gt=0,
                agents__is_active=True,
                agents__is_accepting_orders=True,
            )
            .distinct()
            .order_by("name")
        )
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


class LocationSearchView(LoginRequiredMixin, View):
    def get(self, request):
        query = request.GET.get("q", "")
        try:
            matches = search_addis_locations(query)
        except ValidationError as exc:
            return JsonResponse({"results": [], "message": exc.messages[0]}, status=400)
        return JsonResponse({"results": matches})


class ReverseGeocodeView(LoginRequiredMixin, View):
    def get(self, request):
        latitude = request.GET.get("latitude")
        longitude = request.GET.get("longitude")
        if not latitude or not longitude:
            return JsonResponse({"message": "Latitude and longitude are required."}, status=400)

        try:
            payload = reverse_geocode_coordinate(latitude, longitude)
        except ValidationError as exc:
            return JsonResponse({"message": exc.messages[0]}, status=400)
        return JsonResponse(payload)
