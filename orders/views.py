import base64
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.serializers.json import DjangoJSONEncoder
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from accounts.models import CustomerAddress
from cart.services import get_or_create_cart
from catalog.models import haversine_km
from core.mixins import CustomerRequiredMixin
from core.policies import get_cart_pricing_summary
from orders.forms import CheckoutForm, DeliveryFeedbackForm, RefundRequestForm
from orders.models import Order, OrderStatus, RefundRequestType
from orders.services import (
    cancel_order,
    create_order_request_from_cart,
    expire_order_request_if_needed,
    get_agent_delivery_options,
    initialize_chapa_payment,
    refresh_delivery_confirmation,
    reorder_order_to_cart,
    restore_rejected_order_to_cart,
    request_order_refund,
    skip_delivery_feedback,
    submit_delivery_feedback,
    verify_chapa_payment,
)


TRACKING_ORDER = [
    OrderStatus.REQUESTED,
    OrderStatus.PAYMENT_PENDING,
    OrderStatus.PAID,
    OrderStatus.DRIVER_ASSIGNED,
    OrderStatus.DRIVER_ACCEPTED,
    OrderStatus.PICKED_UP,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.ARRIVED,
    OrderStatus.DELIVERED,
]

logger = logging.getLogger(__name__)


def _float_or_none(value):
    return float(value) if value not in (None, "") else None


def estimate_eta_minutes(lat1, lon1, lat2, lon2):
    distance_km = haversine_km(float(lat1), float(lon1), float(lat2), float(lon2))
    speed_kmh = max(float(getattr(settings, "ETA_AVERAGE_SPEED_KMH", 25)), 1.0)
    eta_minutes = max(1, round((distance_km / speed_kmh) * 60))
    return eta_minutes, round(distance_km, 2)


def build_order_tracking_payload(order):
    expire_order_request_if_needed(order)
    order.refresh_from_db()
    driver_user = getattr(order.assigned_driver, "user", None)
    driver_location = getattr(driver_user, "driver_location", None)
    payload = {
        "orderNumber": order.order_number,
        "statusCode": order.status,
        "statusLabel": order.get_status_display(),
        "selectedAgentName": order.selected_agent.name if order.selected_agent_id else "",
        "rejectionReason": order.rejection_reason,
        "canPay": order.can_make_payment,
        "paymentUrl": reverse("orders:payment", kwargs={"order_number": order.order_number}),
        "agentResponseDeadline": order.agent_response_deadline.isoformat() if order.agent_response_deadline else "",
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
    if getattr(order, "confirmation", None):
        payload["deliveryConfirmation"] = {
            "expiresAt": order.confirmation.expires_at.isoformat() if order.confirmation.expires_at else "",
            "scannedAt": order.confirmation.scanned_at.isoformat() if order.confirmation.scanned_at else "",
        }
    payload["etaMinutes"] = None
    payload["distanceKm"] = None
    if payload["driver"] and payload["driver"]["latitude"] is not None and payload["driver"]["longitude"] is not None:
        payload["etaMinutes"], payload["distanceKm"] = estimate_eta_minutes(
            payload["driver"]["latitude"],
            payload["driver"]["longitude"],
            payload["customer"]["latitude"],
            payload["customer"]["longitude"],
        )
    elif payload["agent"]:
        payload["etaMinutes"], payload["distanceKm"] = estimate_eta_minutes(
            payload["agent"]["latitude"],
            payload["agent"]["longitude"],
            payload["customer"]["latitude"],
            payload["customer"]["longitude"],
        )
    return payload


class CheckoutView(LoginRequiredMixin, CustomerRequiredMixin, FormView):
    template_name = "orders/checkout.html"
    form_class = CheckoutForm

    def dispatch(self, request, *args, **kwargs):
        if not get_or_create_cart(request.user).items.exists():
            messages.info(request, "Your cart is empty. Add products before requesting delivery.")
            return redirect("products:list")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        initial["phone_number"] = self.request.user.phone_number
        default_address = self.request.user.saved_addresses.filter(is_default=True).first()
        if default_address:
            initial["saved_address_id"] = str(default_address.pk)
            initial["delivery_address"] = default_address.address_line
            initial["latitude"] = default_address.latitude
            initial["longitude"] = default_address.longitude
            initial["notes"] = default_address.notes
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
        context["saved_addresses"] = self.request.user.saved_addresses.all()
        return context

    def form_valid(self, form):
        try:
            order = create_order_request_from_cart(self.request.user, form.cleaned_data)
        except Exception as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.info(
            self.request,
            "Please wait a second until the agent confirms your order. Payment will open after the branch accepts it.",
        )
        return redirect("orders:detail", order_number=order.order_number)


class OrderListView(LoginRequiredMixin, CustomerRequiredMixin, ListView):
    model = Order
    template_name = "orders/order_list.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self):
        queryset = self.request.user.orders.select_related(
            "company",
            "selected_agent",
            "assigned_driver__user",
            "feedback",
        ).prefetch_related("refund_requests")
        status = self.request.GET.get("status")
        search = self.request.GET.get("search")
        date_from = self.request.GET.get("date_from")
        date_to = self.request.GET.get("date_to")
        if status:
            queryset = queryset.filter(status=status)
        if search:
            queryset = queryset.filter(order_number__icontains=search)
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        context["query_string"] = query_params.urlencode()
        context["status_choices"] = OrderStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["search_term"] = self.request.GET.get("search", "")
        context["date_from"] = self.request.GET.get("date_from", "")
        context["date_to"] = self.request.GET.get("date_to", "")
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
            "feedback",
        ).prefetch_related("items__product", "status_history", "agent_requests__agent", "refund_requests")

    def get_context_data(self, **kwargs):
        expire_order_request_if_needed(self.object)
        self.object.refresh_from_db()
        context = super().get_context_data(**kwargs)
        context["tracking_steps"] = self.build_tracking_steps(self.object)
        context["can_pay"] = self.object.can_make_payment
        context["order_status_poll_url"] = reverse(
            "orders:tracking_status_json",
            kwargs={"order_number": self.object.order_number},
        )
        context["refund_form"] = RefundRequestForm()
        context["existing_service_refund"] = self.object.refund_requests.filter(
            request_type=RefundRequestType.SERVICE_ISSUE
        ).first()
        context["feedback_form"] = DeliveryFeedbackForm()
        context["feedback_record"] = getattr(self.object, "feedback", None)
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
        if request.method.lower() == "get" and request.GET.get("auto") == "1":
            try:
                payment = initialize_chapa_payment(self.object, request)
            except ValidationError as exc:
                messages.warning(request, exc.messages[0] if exc.messages else "Unable to open Chapa checkout yet.")
            else:
                if payment.checkout_url:
                    return redirect(payment.checkout_url)
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
                "eta_minutes": estimate_eta_minutes(
                    option["agent"].latitude,
                    option["agent"].longitude,
                    latitude,
                    longitude,
                )[0],
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
            message = f"{eligible_count} nearby agent{'s' if eligible_count != 1 else ''} can review and confirm this order before payment."
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
        form = RefundRequestForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Please enter a clear reason for the refund request.")
            return redirect("orders:detail", order_number=order.order_number)

        try:
            request_order_refund(
                order,
                request.user,
                form.cleaned_data["reason"],
                payout_method=form.cleaned_data["payout_method"],
                photos=form.cleaned_data.get("photos", []),
            )
            messages.success(request, "Your refund request was submitted for review.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to submit a refund request.")
        return redirect("orders:detail", order_number=order.order_number)


class ReorderOrderView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.prefetch_related("items__product"),
            order_number=order_number,
        )
        try:
            reorder_order_to_cart(request.user, order)
            messages.success(request, f"{order.order_number} was added back to your cart.")
            return redirect("cart:detail")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to reorder that delivery.")
            return redirect("orders:detail", order_number=order.order_number)


class RetryRejectedOrderCheckoutView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.prefetch_related("items__product"),
            order_number=order_number,
        )
        try:
            restore_rejected_order_to_cart(request.user, order)
            messages.info(request, "Your items are back in the cart. Choose another nearby agent to continue.")
            return redirect("orders:checkout")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to reopen checkout for that order.")
            return redirect("orders:detail", order_number=order.order_number)


class SubmitDeliveryFeedbackView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.select_related("assigned_driver", "selected_agent", "feedback"),
            order_number=order_number,
        )
        form = DeliveryFeedbackForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Please provide a valid rating before submitting your feedback.")
            return redirect("orders:detail", order_number=order.order_number)

        try:
            submit_delivery_feedback(
                order,
                request.user,
                form.cleaned_data["rating"],
                form.cleaned_data.get("comment", ""),
                form.cleaned_data.get("photo"),
            )
            messages.success(request, "Thanks for rating this delivery.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to save your feedback.")
        return redirect("orders:detail", order_number=order.order_number)


class SkipDeliveryFeedbackView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.select_related("assigned_driver", "selected_agent", "feedback"),
            order_number=order_number,
        )
        try:
            skip_delivery_feedback(order, request.user)
            messages.info(request, "Feedback skipped for this delivery.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to skip feedback for this order.")
        return redirect("orders:detail", order_number=order.order_number)


class RefreshDeliveryQRCodeView(LoginRequiredMixin, CustomerRequiredMixin, View):
    def post(self, request, order_number):
        order = get_object_or_404(
            request.user.orders.select_related("confirmation"),
            order_number=order_number,
        )
        if not hasattr(order, "confirmation"):
            messages.error(request, "Your order does not have a delivery QR code yet.")
            return redirect("orders:detail", order_number=order.order_number)
        if order.status not in {
            OrderStatus.PAID,
            OrderStatus.DRIVER_ASSIGNED,
            OrderStatus.DRIVER_ACCEPTED,
            OrderStatus.PICKED_UP,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.ARRIVED,
        }:
            messages.error(request, "A fresh QR code can only be requested while delivery is still in progress.")
            return redirect("orders:detail", order_number=order.order_number)

        refresh_delivery_confirmation(order, force=True)
        messages.success(request, "A fresh delivery QR code has been generated.")
        return redirect("orders:detail", order_number=order.order_number)


@method_decorator(csrf_exempt, name="dispatch")
class ChapaPaymentCallbackView(View):
    @staticmethod
    def _get_webhook_signature(request):
        return (
            request.headers.get("Chapa-Signature")
            or request.META.get("HTTP_CHAPA_SIGNATURE")
            or ""
        ).strip()

    @staticmethod
    def _signature_matches(request):
        secret = (getattr(settings, "CHAPA_WEBHOOK_SECRET", "") or "").strip()
        if not secret:
            raise ValidationError("Chapa webhook secret is not configured.")

        submitted_signature = ChapaPaymentCallbackView._get_webhook_signature(request)
        if not submitted_signature:
            raise ValidationError("Missing Chapa webhook signature.")

        normalized_signature = submitted_signature
        if "=" in normalized_signature:
            normalized_signature = normalized_signature.split("=", 1)[1].strip()

        digest = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).digest()
        expected_hex = digest.hex()
        expected_b64 = base64.b64encode(digest).decode("ascii")

        return any(
            hmac.compare_digest(candidate, normalized_signature)
            for candidate in (expected_hex, expected_hex.upper(), expected_b64)
        )

    @staticmethod
    def _extract_tx_ref(payload):
        if not isinstance(payload, dict):
            return ""
        data = payload.get("data")
        if isinstance(data, dict):
            tx_ref = data.get("tx_ref") or data.get("trx_ref")
            if tx_ref:
                return tx_ref
        return payload.get("tx_ref") or payload.get("trx_ref") or ""

    def get(self, request):
        tx_ref = (
            request.GET.get("tx_ref")
            or request.GET.get("trx_ref")
            or request.POST.get("tx_ref")
            or request.POST.get("trx_ref")
        )
        if not tx_ref:
            return HttpResponseBadRequest("Invalid payment callback request.")

        try:
            confirmation = verify_chapa_payment(tx_ref)
        except Exception:
            logger.exception("Failed to verify Chapa payment callback for %s", tx_ref)
            return HttpResponseBadRequest("Unable to verify payment callback.")

        return redirect("orders:detail", order_number=confirmation.order.order_number)

    def post(self, request):
        try:
            if not self._signature_matches(request):
                return HttpResponseForbidden("Invalid webhook signature.")
        except ValidationError:
            logger.warning("Rejected Chapa webhook with missing or invalid signature.")
            return HttpResponseForbidden("Invalid webhook signature.")

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Invalid webhook payload.")

        tx_ref = self._extract_tx_ref(payload)
        if not tx_ref:
            return HttpResponseBadRequest("Invalid webhook payload.")

        try:
            confirmation = verify_chapa_payment(tx_ref)
        except Exception:
            logger.exception("Failed to verify signed Chapa webhook for %s", tx_ref)
            return HttpResponseBadRequest("Unable to verify payment callback.")

        return JsonResponse(
            {
                "status": "ok",
                "order_number": confirmation.order.order_number,
            }
        )


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
