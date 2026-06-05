from django.urls import path

from cart.views import AddToCartView, CartDetailView, RemoveCartItemView, UpdateCartItemView

app_name = "cart"

urlpatterns = [
    path("", CartDetailView.as_view(), name="detail"),
    path("add/<slug:slug>/", AddToCartView.as_view(), name="add"),
    path("items/<int:pk>/update/", UpdateCartItemView.as_view(), name="update"),
    path("items/<int:pk>/remove/", RemoveCartItemView.as_view(), name="remove"),
]
