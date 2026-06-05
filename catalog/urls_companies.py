from django.urls import path

from catalog.views import CompanyListView

app_name = "companies"

urlpatterns = [
    path("", CompanyListView.as_view(), name="list"),
]
