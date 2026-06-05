from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from cart.forms import AddToCartForm, UpdateCartItemForm
from cart.models import CartItem
from cart.services import add_product_to_cart, get_or_create_cart, remove_cart_item, update_cart_item
from catalog.models import Product
from core.mixins import CustomerRequiredMixin
from core.policies import get_cart_pricing_summary


class CartDetailView(LoginRequiredMixin, CustomerRequiredMixin, TemplateView):
    template_name = "cart/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cart = get_or_create_cart(self.request.user)
        context["cart"] = cart
        context["pricing_summary"] = get_cart_pricing_summary(cart)
        context["update_form"] = UpdateCartItemForm()
        return context


class AddToCartView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, slug):
        product = get_object_or_404(Product, slug=slug, is_active=True)
        form = AddToCartForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please enter a valid quantity.")
            return redirect("products:detail", slug=product.slug)

        try:
            add_product_to_cart(request.user, product, form.cleaned_data["quantity"])
            messages.success(request, f"{product.name} has been added to your cart.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update cart.")
            return redirect("products:detail", slug=product.slug)

        if request.POST.get("action") == "buy_now":
            return redirect("orders:checkout")
        return redirect("cart:detail")


class UpdateCartItemView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(CartItem.objects.select_related("cart"), pk=pk, cart__user=request.user)
        form = UpdateCartItemForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please enter a valid quantity.")
            return redirect("cart:detail")

        try:
            update_cart_item(item, form.cleaned_data["quantity"])
            messages.success(request, "Cart item updated.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to update the cart item.")

        return redirect("cart:detail")


class RemoveCartItemView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(CartItem.objects.select_related("cart"), pk=pk, cart__user=request.user)
        remove_cart_item(item)
        messages.success(request, "Cart item removed.")
        return redirect("cart:detail")
