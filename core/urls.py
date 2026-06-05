from django.urls import path

from core.views import LocationSearchView, ReverseGeocodeView

app_name = "core"

urlpatterns = [
    path("search/", LocationSearchView.as_view(), name="location_search"),
    path("reverse/", ReverseGeocodeView.as_view(), name="reverse_geocode"),
]
