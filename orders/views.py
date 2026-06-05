from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.serializers.json import DjangoJSONEncoder
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from cart.services import get_or_create_cart
from core.mixins import CustomerRequiredMixin
from core.policies import get_cart_pricing_summary
from orders.forms import CheckoutForm, RefundRequestForm
from orders.models import Order, OrderStatus, RefundRequestType
from orders.services import (
    cancel_order,
    create_order_request_from_cart,
    get_agent_delivery_options,
    initialize_chapa_payment,
    request_order_refund,
    verify_chapa_payment,
)


TRACKING_ORDER = [
    OrderStatus.REQUESTED,
    OrderStatus.PAYMENT_PENDING,
    OrderStatus.PAID,
    OrderStatus.DRIVER_ASSIGNED,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
]


def _float_or_none(value):
    return float(value) if value not in (None, "") else None


def build_order_tracking_payload(order):
    driver_user = getattr(order.assigned_driver, "user", None)
    driver_location = getattr(driver_user, "driver_location", None)
    payload = {
        "orderNumber": order.order_number,
        "statusCode": order.status,
        "statusLabel": order.get_status_display(),
        "customer": {
            "name": order.customer.full_name,
            "address": order.delivery_address,
            "latitude": float(order.latitude),
            "longitude": float(order.longitude),
        },
        "agent": None,
        "driver": None,
    }
    if order.selected_agent_id:
        payload["agent"] = {
            "name": order.selected_agent.name,
            "address": order.selected_agent.address,
            "locationName": order.selected_agent.location_name,
            "latitude": float(order.selected_agent.latitude),
            "longitude": float(order.selected_agent.longitude),
        }
    if order.assigned_driver_id:
        payload["driver"] = {
            "name": order.assigned_driver.user.full_name,
            "vehicleIdentifier": order.assigned_driver.vehicle_identifier,
            "online": bool(driver_location and driver_location.is_online),
            "lastPingAt": driver_location.last_ping_at.isoformat() if driver_location else "",
            "latitude": _float_or_none(getattr(driver_location, "latitude", None)),
            "longitude": _float_or_none(getattr(driver_location, "longitude", None)),
        }
    return payload


class CheckoutView(LoginRequiredMixin, CustomerRequiredMixin, FormView):
    template_name = "orders/checkout.html"
    form_class = CheckoutForm

    def dispatch(self, request, *args, **kwargs):
        if not get_or_create_cart(request.user).items.exists():
            messages.info(request, "Your cart is empty. Add products before requesting delivery.")
            return redirect("products:list")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        initial["phone_number"] = self.request.user.phone_number
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cart = get_or_create_cart(self.request.user)
        context["cart"] = cart
        context["pricing_summary"] = get_cart_pricing_summary(cart)
        context["cart_company"] = cart.company
        context["checkout_map_config"] = {
            "defaultCenter": [9.03, 38.74],
            "nearbyAgentsUrl": reverse("orders:nearby_agents_preview"),
            "searchUrl": reverse("core:location_search"),
            "reverseUrl": reverse("core:reverse_geocode"),
        }
        return context

    def form_valid(self, form):
        try:
            order = create_order_request_from_cart(self.request.user, form.cleaned_data)
        except Exception as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)

        try:
            payment = initialize_chapa_payment(order, self.request)
        except ValidationError as exc:
            messages.warning(
                self.request,
                exc.messages[0] if exc.messages else "Your order was created, but we could not open Chapa yet.",
            )
            return redirect("orders:payment", order_number=order.order_number)

        return redirect(payment.checkout_url)


class OrderListView(LoginRequiredMixin, CustomerRequiredMixin, ListView):
    model = Order
    template_name = "orders/order_list.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self):
        queryset = self.request.user.orders.select_related("company", "selected_agent", "assigned_driver")
        status = self.request.GET.get("status")
        search = self.request.GET.get("search")
        if status:
            queryset = queryset.filter(status=status)
        if search:
            queryset = queryset.filter(order_number__icontains=search)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["query_string"] = query_params.urlencode()
        context["status_choices"] = OrderStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["search_term"] = self.request.GET.get("search", "")
        return context


class OrderDetailView(LoginRequiredMixin, CustomerRequiredMixin, DetailView):
    model = Order
    template_name = "orders/order_detail.html"
    context_object_name = "order"
    slug_field = "order_number"
    slug_url_kwarg = "order_number"

    def get_queryset(self):
        return self.request.user.orders.select_related(
            "company",
            "selected_agent",
            "assigned_driver__user",
            "payment",
            "confirmation",
        ).prefetch_related("items__product", "status_history", "agent_requests__agent", "refund_requests")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tracking_steps"] = self.build_tracking_steps(self.object)
        context["can_pay"] = self.object.can_make_payment
        context["refund_form"] = RefundRequestForm()
        context["existing_service_refund"] = self.object.refund_requests.filter(
            request_type=RefundRequestType.SERVICE_ISSUE
        ).first()
        return context

    @staticmethod
    def build_tracking_steps(order):
        current_index = TRACKING_ORDER.index(order.status) if order.status in TRACKING_ORDER else -1
        return [
            {
                "code": step,
                "label": dict(OrderStatus.choices)[step],
                "complete": index <= current_index,
                "current": step == order.status,
            }
            for index, step in enumerate(TRACKING_ORDER)
        ]


class OrderPaymentView(LoginRequiredMixin, CustomerRequiredMixin, DetailView):
    model = Order
    template_name = "orders/payment.html"
    context_object_name = "order"
    slug_field = "order_number"
    slug_url_kwarg = "order_number"

    def get_queryset(self):
        return self.request.user.orders.select_related("payment", "selected_agent", "company")

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.can_make_payment:
            messages.info(request, "Payment is only available after an agent accepts your order.")
            return redirect("orders:detail", order_number=self.object.order_number)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        try:
            payment = initialize_chapa_payment(self.object, request, force_refresh=True)
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to initialize Chapa checkout.")
            return redirect("orders:payment", order_number=self.object.order_number)
        return redirect(payment.checkout_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pricing_summary"] = {
            "subtotal": self.object.subtotal,
            "discount_amount": self.object.discount_amount,
            "delivery_fee": self.object.delivery_fee,
            "total": self.object.total,
        }
        checkout_error = None
        if not hasattr(self.object, "payment") or not self.object.payment.checkout_url:
            try:
                initialize_chapa_payment(self.object, self.request)
                self.object.refresh_from_db()
            except ValidationError as exc:
                checkout_error = exc.messages[0] if exc.messages else "Unable to initialize Chapa payment."
        context["checkout_ready"] = bool(getattr(getattr(self.object, "payment", None), "checkout_url", ""))
        context["checkout_error"] = checkout_error
        return context


class OrderTrackingView(LoginRequiredMixin, CustomerRequiredMixin, DetailView):
    model = Order
    template_name = "orders/order_tracking.html"
    context_object_name = "order"
    slug_field = "order_number"
    slug_url_kwarg = "order_number"

    def get_queryset(self):
        return self.request.user.orders.select_related(
            "selected_agent",
            "assigned_driver__user__driver_location",
            "confirmation",
        ).prefetch_related(
            "status_history"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tracking_steps"] = OrderDetailView.build_tracking_steps(self.object)
        context["tracking_map_payload"] = build_order_tracking_payload(self.object)
        context["tracking_map_data_url"] = reverse("orders:tracking_status_json", kwargs={"order_number": self.object.order_number})
        return context


class OrderTrackingStatusView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def get(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.select_related("selected_agent", "assigned_driver__user__driver_location"),
            order_number=order_number,
        )
        return JsonResponse(build_order_tracking_payload(order), encoder=DjangoJSONEncoder)


class NearbyAgentsPreviewView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def get(self, request):
        cart = get_or_create_cart(request.user)
        items = list(cart.items.select_related("product", "product__company"))
        company = cart.company
        if not items or not company:
            return JsonResponse({"agents": [], "message": "Add products to your cart first."})

        try:
            latitude = float(request.GET.get("latitude", ""))
            longitude = float(request.GET.get("longitude", ""))
        except (TypeError, ValueError):
            return JsonResponse({"agents": [], "message": "Choose a valid location first."}, status=400)

        delivery_options = get_agent_delivery_options(company, items, latitude, longitude)
        payload = [
            {
                "id": option["agent"].pk,
                "name": option["agent"].name,
                "location_name": option["agent"].location_name,
                "address": option["agent"].address,
                "latitude": float(option["agent"].latitude),
                "longitude": float(option["agent"].longitude),
                "distance_km": float(option["distance_km"]),
                "service_radius_km": float(option["agent"].service_radius_km),
                "within_radius": option["within_radius"],
                "has_stock": option["has_stock"],
                "is_eligible": option["is_eligible"],
                "unavailable_reason": option["unavailable_reason"],
            }
            for option in delivery_options
        ]
        message = ""
        nearby_count = sum(1 for option in delivery_options if option["within_radius"])
        eligible_count = sum(1 for option in delivery_options if option["is_eligible"])
        if not payload:
            message = "No active agents are available for this company right now."
        elif eligible_count:
            message = f"{eligible_count} nearby agent{'s' if eligible_count != 1 else ''} can take payment for this order."
        elif nearby_count:
            message = "Nearby agents were found, but none currently have enough stock for this order."
        else:
            message = "Agents exist for this company, but your selected point is outside their delivery radius."
        return JsonResponse({"agents": payload, "message": message})


class CancelOrderView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(request.user.orders.select_related("payment", "selected_agent"), order_number=order_number)
        try:
            cancel_order(order, request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Your order was cancelled successfully.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to cancel this order.")
        return redirect("orders:detail", order_number=order.order_number)


class RefundRequestCreateView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.select_related("payment", "company__admin"),
            order_number=order_number,
        )
        form = RefundRequestForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please enter a clear reason for the refund request.")
            return redirect("orders:detail", order_number=order.order_number)

        try:
            request_order_refund(order, request.user, form.cleaned_data["reason"])
            messages.success(request, "Your refund request was submitted for review.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to submit a refund request.")
        return redirect("orders:detail", order_number=order.order_number)


class ChapaPaymentCallbackView(View):
    def get(self, request):
        tx_ref = (
            request.GET.get("tx_ref")
            or request.GET.get("trx_ref")
            or request.POST.get("tx_ref")
            or request.POST.get("trx_ref")
        )
        if not tx_ref:
            return HttpResponseBadRequest("Missing transaction reference.")

        try:
            confirmation = verify_chapa_payment(tx_ref)
        except Exception as exc:
            return HttpResponseBadRequest(str(exc))

        return redirect("orders:detail", order_number=confirmation.order.order_number)

    post = get


class ChapaPaymentReturnView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def get(self, request, order_number):
        order = get_object_or_404(request.user.orders.select_related("payment"), order_number=order_number)
        tx_ref = request.GET.get("tx_ref") or request.GET.get("trx_ref")
        if not tx_ref and hasattr(order, "payment") and order.payment.reference and order.status != OrderStatus.PAID:
            tx_ref = order.payment.reference
        if tx_ref:
            try:
                verify_chapa_payment(tx_ref)
                messages.success(request, "Payment verified successfully.")
            except Exception as exc:
                messages.warning(request, f"We could not verify payment yet: {exc}")
        return redirect("orders:detail", order_number=order.order_number)
