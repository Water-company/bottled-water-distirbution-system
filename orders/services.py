import uuid
import json
from decimal import Decimal
from datetime import timezone as dt_timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from accounts.services import get_company_admin_users
from core.services import notify_user
from core.policies import get_cart_pricing_summary, quantize_money
from cart.services import add_product_to_cart, get_or_create_cart
from catalog.models import Agent, AgentStock, InventoryBatch, InventoryTransactionType
from catalog.services import create_inventory_transaction
from orders.models import (
    AgentRequestStatus,
    DeliveryFeedback,
    DeliveryIssue,
    DeliveryIssueType,
    DeliveryConfirmation,
    Order,
    OrderAgentRequest,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    RefundRequest,
    RefundEvidence,
    RefundPayoutMethod,
    RefundRequestStatus,
    RefundRequestType,
    OrderStatusHistory,
)
from orders.qr_tokens import QRTokenError, build_customer_token_id, decode_signed_qr_token


ACTIVE_DELIVERY_STATUSES = {
    OrderStatus.PAID,
    OrderStatus.DRIVER_ASSIGNED,
    OrderStatus.DRIVER_ACCEPTED,
    OrderStatus.PICKED_UP,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.ARRIVED,
}

RESERVED_STOCK_ORDER_STATUSES = ACTIVE_DELIVERY_STATUSES | {OrderStatus.PAYMENT_PENDING}
QR_CONFIRMABLE_STATUSES = {OrderStatus.ARRIVED}


class QRConfirmationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def get_cart_company(cart):
    first_item = cart.items.select_related("product__company").first()
    return first_item.product.company if first_item else None


@transaction.atomic
def reorder_order_to_cart(user, order):
    if order.customer_id != user.id:
        raise ValidationError("You can only reorder your own deliveries.")
    if order.status != OrderStatus.DELIVERED:
        raise ValidationError("Only delivered orders can be reordered.")
    if not order.items.exists():
        raise ValidationError("This order does not have any items to reorder.")

    cart = get_or_create_cart(user)
    cart.items.all().delete()
    unavailable_products = []
    for item in order.items.select_related("product"):
        if not item.product.is_active:
            unavailable_products.append(item.product_name)
            continue
        add_product_to_cart(user, item.product, item.quantity)

    if unavailable_products:
        raise ValidationError(
            f"Some products are no longer available for reorder: {', '.join(unavailable_products)}."
        )
    return cart


@transaction.atomic
def restore_rejected_order_to_cart(user, order):
    if order.customer_id != user.id:
        raise ValidationError("You can only retry your own orders.")
    if order.status != OrderStatus.REJECTED:
        raise ValidationError("You can only choose another agent after an order has been rejected.")
    if not order.items.exists():
        raise ValidationError("This order does not have any items to retry.")

    cart = get_or_create_cart(user)
    cart.items.all().delete()
    unavailable_products = []
    for item in order.items.select_related("product"):
        if not item.product.is_active:
            unavailable_products.append(item.product_name)
            continue
        add_product_to_cart(user, item.product, item.quantity)

    if unavailable_products:
        raise ValidationError(
            f"Some products are no longer available for retry: {', '.join(unavailable_products)}."
        )
    return cart


def get_eligible_agents(company, items, latitude, longitude):
    eligible_agents = []
    for option in get_agent_delivery_options(company, items, latitude, longitude):
        if option["is_eligible"]:
            eligible_agents.append((option["agent"], option["distance_km"]))
    return eligible_agents


def get_agent_delivery_options(company, items, latitude, longitude):
    options = []
    if not company.is_live:
        return options
    agents = (
        Agent.objects.filter(company=company, is_active=True, is_accepting_orders=True)
        .prefetch_related("stocks__product")
    )
    for agent in agents:
        distance_value = agent.distance_to(latitude, longitude)
        distance_km = Decimal(str(round(distance_value, 2)))
        within_radius = distance_value <= float(agent.service_radius_km)
        stocks = {
            stock.product_id: stock
            for stock in agent.stocks.all()
        }
        missing_items = [
            item.product.name
            for item in items
            if item.product_id not in stocks or stocks[item.product_id].available_quantity < item.quantity
        ]
        has_stock = not missing_items
        if within_radius and has_stock:
            unavailable_reason = ""
        elif not within_radius and not has_stock:
            unavailable_reason = "Outside service radius and does not have enough stock."
        elif not within_radius:
            unavailable_reason = "Outside this agent's delivery radius."
        else:
            unavailable_reason = "Nearby, but does not have enough stock for this order."

        options.append(
            {
                "agent": agent,
                "distance_km": distance_km,
                "within_radius": within_radius,
                "has_stock": has_stock,
                "is_eligible": within_radius and has_stock,
                "unavailable_reason": unavailable_reason,
                "missing_items": missing_items,
            }
        )

    options.sort(
        key=lambda option: (
            0 if option["within_radius"] else 1,
            0 if option["has_stock"] else 1,
            option["distance_km"],
            option["agent"].name.lower(),
        )
    )
    return options


def _set_latest_status_note(order, note):
    latest_history = order.status_history.order_by("-created_at").first()
    if latest_history and latest_history.status == order.status:
        latest_history.note = note
        latest_history.save(update_fields=["note"])


@transaction.atomic
def expire_order_request_if_needed(order):
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != OrderStatus.REQUESTED or not order.agent_response_deadline:
        return False

    now = timezone.now()
    if order.agent_response_deadline > now:
        return False

    pending_requests = list(order.agent_requests.select_for_update().filter(status=AgentRequestStatus.PENDING))
    if not pending_requests:
        order.agent_response_deadline = None
        order.save(update_fields=["agent_response_deadline", "updated_at"])
        return False

    timeout_note = "The selected agent did not confirm this order within the response window."
    for pending_request in pending_requests:
        pending_request.status = AgentRequestStatus.REJECTED
        pending_request.note = timeout_note
        pending_request.responded_at = now
        pending_request.save(update_fields=["status", "note", "responded_at", "updated_at"])

    order.status = OrderStatus.REJECTED
    order.rejected_at = now
    order.agent_response_deadline = None
    order.rejection_reason = timeout_note
    order.save()
    _set_latest_status_note(order, timeout_note)
    notify_user(
        order.customer,
        "Order request timed out",
        f"{order.order_number} was not confirmed in time. Please choose another agent and try again.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    return True


def release_reserved_stock(order):
    if not order.selected_agent_id or order.status not in RESERVED_STOCK_ORDER_STATUSES:
        return

    for item in order.items.select_related("product"):
        stock, _ = AgentStock.objects.select_for_update().get_or_create(
            agent=order.selected_agent,
            product=item.product,
            defaults={"available_quantity": 0, "reorder_level": 0},
        )
        stock.available_quantity += item.quantity
        stock.save(update_fields=["available_quantity", "updated_at"])


def _reserve_stock_for_items(agent, items):
    required_stocks = []
    for item in items:
        stock = AgentStock.objects.select_for_update().filter(agent=agent, product=item.product).first()
        product_name = getattr(item, "product_name", "") or item.product.name
        if not stock or stock.available_quantity < item.quantity:
            raise ValidationError(f"{agent.name} no longer has enough stock for {product_name}.")
        required_stocks.append((stock, item.quantity))

    for stock, quantity in required_stocks:
        stock.available_quantity -= quantity
        stock.save(update_fields=["available_quantity", "updated_at"])


def _sync_inventory_batches_for_item(agent, item, stock, active_reserved_quantity):
    batches = list(
        InventoryBatch.objects.select_for_update().filter(
            agent=agent,
            product=item.product,
            quantity_remaining__gt=0,
        ).order_by("expires_at", "received_at", "created_at")
    )
    batch_available_quantity = sum(batch.quantity_remaining for batch in batches)
    expected_total_quantity = stock.available_quantity + active_reserved_quantity + item.quantity
    missing_quantity = max(0, expected_total_quantity - batch_available_quantity)

    if missing_quantity:
        today = timezone.localdate()
        sync_batch, created = InventoryBatch.objects.get_or_create(
            agent=agent,
            batch_number=f"AUTO-SYNC-{item.product_id}",
            defaults={
                "product": item.product,
                "quantity_received": missing_quantity,
                "quantity_remaining": missing_quantity,
                "base_unit_cost": item.unit_price,
                "expires_at": today + timezone.timedelta(days=365),
                "received_at": today,
            },
        )
        if not created:
            sync_batch.product = item.product
            sync_batch.quantity_received += missing_quantity
            sync_batch.quantity_remaining += missing_quantity
            sync_batch.base_unit_cost = item.unit_price
            sync_batch.received_at = today
            if sync_batch.expires_at < today:
                sync_batch.expires_at = today + timezone.timedelta(days=365)
            sync_batch.save(
                update_fields=[
                    "product",
                    "quantity_received",
                    "quantity_remaining",
                    "base_unit_cost",
                    "received_at",
                    "expires_at",
                    "updated_at",
                ]
            )
        batches = list(
            InventoryBatch.objects.select_for_update().filter(
                agent=agent,
                product=item.product,
                quantity_remaining__gt=0,
            ).order_by("expires_at", "received_at", "created_at")
        )
    return batches


@transaction.atomic
def create_order_request_from_cart(user, cleaned_data):
    cart = get_or_create_cart(user)
    items = list(cart.items.select_related("product", "product__company"))
    if not items:
        raise ValidationError("Your cart is empty.")

    company = get_cart_company(cart)
    if not company:
        raise ValidationError("Your cart is missing company information.")
    if not company.is_live:
        raise ValidationError("This company is not currently accepting new customer orders.")

    if any(item.product.company_id != company.id for item in items):
        raise ValidationError("All cart items must belong to the same company.")

    latitude = cleaned_data["latitude"]
    longitude = cleaned_data["longitude"]
    selected_agent_id = cleaned_data.get("selected_agent_id")
    delivery_options = get_agent_delivery_options(company, items, latitude, longitude)
    eligible_agents = [option for option in delivery_options if option["is_eligible"]]
    if not eligible_agents:
        raise ValidationError(
            "No nearby agents from this company can currently fulfill this order at the selected location."
        )

    try:
        selected_agent_id = int(selected_agent_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Please choose a valid nearby agent before continuing to payment.") from exc

    selected_option = next(
        (option for option in eligible_agents if option["agent"].pk == selected_agent_id),
        None,
    )
    if selected_option is None:
        raise ValidationError("The selected agent is no longer eligible for this delivery. Please choose another nearby agent.")

    selected_agent = selected_option["agent"]
    selected_distance = selected_option["distance_km"]
    pricing_summary = get_cart_pricing_summary(cart)
    now = timezone.now()
    total_units = sum(item.quantity for item in items)
    primary_product = items[0].product.name if items else "water products"

    order = Order.objects.create(
        customer=user,
        company=company,
        selected_agent=selected_agent,
        location_source=cleaned_data["location_source"],
        delivery_address=cleaned_data["delivery_address"],
        latitude=latitude,
        longitude=longitude,
        phone_number=cleaned_data["phone_number"],
        notes=cleaned_data.get("notes", ""),
        subtotal=pricing_summary["subtotal"],
        discount_amount=pricing_summary["discount_amount"],
        premium_discount_percent=pricing_summary["premium_offer"]["discount_percent"],
        premium_streak_count=pricing_summary["premium_offer"]["streak"],
        delivery_fee=pricing_summary["delivery_fee"],
        total=pricing_summary["total"],
        status=OrderStatus.REQUESTED,
        agent_response_deadline=now + timezone.timedelta(minutes=settings.AGENT_REQUEST_RESPONSE_MINUTES),
    )

    for item in items:
        OrderItem.objects.create(
            order=order,
            product=item.product,
            product_name=item.product.name,
            unit_price=item.unit_price,
            quantity=item.quantity,
        )

    OrderAgentRequest.objects.create(
        order=order,
        agent=selected_agent,
        status=AgentRequestStatus.PENDING,
        distance_km=selected_distance,
        note="Chosen by the customer during checkout and waiting for agent confirmation.",
    )

    cart.items.all().delete()
    notify_user(
        user,
        "Order request submitted",
        f"{order.order_number} was sent to {selected_agent.name}. Please wait while the agent confirms the order.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    if selected_agent.admin:
        notify_user(
            selected_agent.admin,
            "New customer order request",
            f"You have a new order of {total_units} units of {primary_product} from {user.full_name}. Accept or decline {order.order_number} to continue.",
            link=reverse("accounts:agent_dashboard"),
        )
    else:
        for admin_user in get_company_admin_users(company):
            notify_user(
                admin_user,
                "New customer order request",
                f"You have a new order of {total_units} units of {primary_product} from {user.full_name}.",
                link=reverse("accounts:company_dashboard"),
            )
    _set_latest_status_note(
        order,
        f"Waiting for {selected_agent.name} to confirm the order before payment. Response window: {settings.AGENT_REQUEST_RESPONSE_MINUTES} minutes.",
    )
    return order


@transaction.atomic
def accept_agent_request(agent_request, note="", accepted_by=None):
    order = Order.objects.select_for_update().get(pk=agent_request.order_id)
    if order.status != OrderStatus.REQUESTED:
        raise ValidationError("This order is no longer waiting for agent review.")

    order_items = list(order.items.select_related("product"))
    _reserve_stock_for_items(agent_request.agent, order_items)

    now = timezone.now()
    agent_request.status = AgentRequestStatus.ACCEPTED
    agent_request.note = note
    agent_request.responded_at = now
    agent_request.save(update_fields=["status", "note", "responded_at", "updated_at"])

    order.selected_agent = agent_request.agent
    order.status = OrderStatus.PAYMENT_PENDING
    order.accepted_at = now
    order.agent_response_deadline = None
    order.rejection_reason = ""
    order.save()
    _set_latest_status_note(order, note or "Agent accepted the order. Payment can now begin.")
    if order.customer:
        notify_user(
            order.customer,
            "Order accepted",
            f"{order.order_number} was accepted by {agent_request.agent.name}. Chapa payment is now ready.",
            link=reverse("orders:payment", kwargs={"order_number": order.order_number}),
        )

    OrderAgentRequest.objects.filter(order=order, status=AgentRequestStatus.PENDING).exclude(
        pk=agent_request.pk
    ).update(
        status=AgentRequestStatus.REJECTED,
        note="Another nearby agent accepted this order.",
        responded_at=now,
    )

    payment, _ = Payment.objects.update_or_create(
        order=order,
        defaults={
            "provider": PaymentProvider.CHAPA,
            "status": PaymentStatus.PENDING,
            "amount": order.total,
            "reference": generate_payment_reference(order),
        },
    )
    return payment


@transaction.atomic
def reject_agent_request(agent_request, note=""):
    order = Order.objects.select_for_update().get(pk=agent_request.order_id)
    if order.status != OrderStatus.REQUESTED:
        raise ValidationError("This order is no longer waiting for agent review.")

    now = timezone.now()
    agent_request.status = AgentRequestStatus.REJECTED
    agent_request.note = note
    agent_request.responded_at = now
    agent_request.save(update_fields=["status", "note", "responded_at", "updated_at"])

    if not order.agent_requests.filter(status=AgentRequestStatus.PENDING).exists():
        order.status = OrderStatus.REJECTED
        order.rejected_at = now
        order.agent_response_deadline = None
        order.rejection_reason = note or "All nearby agents declined the request."
        order.save()
        _set_latest_status_note(order, order.rejection_reason)
        if order.customer:
            notify_user(
                order.customer,
                "Order rejected",
                f"{order.order_number} could not be accepted by nearby agents.",
                link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
            )

    return order


@transaction.atomic
def refresh_delivery_confirmation(order, force=False):
    confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
    if force or confirmation.is_expired or not confirmation.qr_code_image or not confirmation.qr_token:
        confirmation.refresh_qr_assets()
    return confirmation


@transaction.atomic
def mark_order_paid(order, reference=None, payload=None):
    if order.status == OrderStatus.PAID:
        payment = getattr(order, "payment", None)
        if payment and payload:
            payment.raw_payload = payload
            payment.save(update_fields=["raw_payload", "updated_at"])
        if not order.cancellation_deadline:
            order.cancellation_deadline = (order.paid_at or timezone.now()) + timezone.timedelta(
                minutes=settings.ORDER_CANCELLATION_WINDOW_MINUTES
            )
            order.save(update_fields=["cancellation_deadline", "updated_at"])
        confirmation = refresh_delivery_confirmation(order)
        return confirmation

    if order.status != OrderStatus.PAYMENT_PENDING:
        raise ValidationError("This order is not ready for payment.")

    now = timezone.now()
    payment, _ = Payment.objects.get_or_create(
        order=order,
        defaults={
            "provider": PaymentProvider.CHAPA,
            "status": PaymentStatus.PENDING,
            "amount": order.total,
            "reference": reference or generate_payment_reference(order),
        },
    )
    payment.status = PaymentStatus.PAID
    payment.reference = reference or payment.reference
    payment.amount = order.total
    payment.paid_at = now
    payment.raw_payload = payload or payment.raw_payload
    payment.save()

    order.status = OrderStatus.PAID
    order.paid_at = now
    order.cancellation_deadline = now + timezone.timedelta(minutes=settings.ORDER_CANCELLATION_WINDOW_MINUTES)
    order.save()
    _set_latest_status_note(
        order,
        f"Payment confirmed. Cancellation remains available until {timezone.localtime(order.cancellation_deadline).strftime('%Y-%m-%d %H:%M')}.",
    )

    confirmation = refresh_delivery_confirmation(order, force=True)
    send_delivery_confirmation_email(order, confirmation)
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Payment confirmed",
            f"{order.order_number} has been paid and is ready for driver assignment.",
            link=reverse("accounts:agent_dashboard"),
        )
    send_order_status_email(
        order,
        "Payment confirmed",
        "Your order has been confirmed and a delivery QR code is now ready on your order page.",
    )
    return confirmation


def initialize_chapa_payment(order, request, force_refresh=False):
    if not settings.CHAPA_SECRET_KEY:
        raise ValidationError("Chapa secret key is not configured.")
    if order.status not in {OrderStatus.PAYMENT_PENDING, OrderStatus.PAID}:
        raise ValidationError("This order is not ready for payment.")

    payment, _ = Payment.objects.get_or_create(
        order=order,
        defaults={
            "provider": PaymentProvider.CHAPA,
            "status": PaymentStatus.PENDING,
            "amount": order.total,
            "reference": generate_payment_reference(order),
        },
    )
    if payment.status == PaymentStatus.PAID or order.status == OrderStatus.PAID:
        return payment

    if payment.checkout_url and not force_refresh and payment.status == PaymentStatus.PENDING:
        return payment

    if force_refresh or payment.status in {PaymentStatus.FAILED, PaymentStatus.CANCELLED}:
        payment.reference = generate_payment_reference(order)
        payment.checkout_url = ""

    payload = {
        "amount": str(order.total),
        "currency": "ETB",
        "email": order.customer.email,
        "first_name": order.customer.first_name or "Water",
        "last_name": order.customer.last_name or "Customer",
        "phone_number": order.phone_number,
        "tx_ref": payment.reference,
        "callback_url": request.build_absolute_uri(reverse("orders:payment_callback")),
        "return_url": request.build_absolute_uri(
            reverse("orders:payment_success", kwargs={"order_number": order.order_number})
        ),
        "customization": {
            "title": "Water Delivery",
            "description": f"Payment for order {order.order_number}",
        },
        "meta": {
            "order_number": order.order_number,
            "company_name": order.company.name,
        },
    }
    response = chapa_request("POST", "/transaction/initialize", payload)
    checkout_url = response.get("data", {}).get("checkout_url")
    if response.get("status") != "success" or not checkout_url:
        raise ValidationError(response.get("message") or "Unable to initialize Chapa payment.")

    payment.status = PaymentStatus.PENDING
    payment.amount = order.total
    payment.checkout_url = checkout_url
    payment.raw_payload = response
    payment.save(update_fields=["reference", "status", "amount", "checkout_url", "raw_payload", "updated_at"])
    return payment


def verify_chapa_payment(tx_ref):
    if not settings.CHAPA_SECRET_KEY:
        raise ValidationError("Chapa secret key is not configured.")

    try:
        payment = Payment.objects.select_related("order").get(reference=tx_ref)
    except Payment.DoesNotExist as exc:
        raise ValidationError("We could not find a Chapa payment for that transaction reference.") from exc
    response = chapa_request("GET", f"/transaction/verify/{tx_ref}")
    if response.get("status") != "success":
        payment.status = PaymentStatus.FAILED
        payment.raw_payload = response
        payment.checkout_url = ""
        payment.save(update_fields=["status", "raw_payload", "checkout_url", "updated_at"])
        raise ValidationError(response.get("message") or "Unable to verify Chapa payment.")

    data = response.get("data", {})
    upstream_status = (data.get("status") or "").lower()
    if upstream_status != "success":
        payment.status = {
            "failed": PaymentStatus.FAILED,
            "cancelled": PaymentStatus.CANCELLED,
        }.get(upstream_status, PaymentStatus.PENDING)
        payment.raw_payload = response
        if payment.status in {PaymentStatus.FAILED, PaymentStatus.CANCELLED}:
            payment.checkout_url = ""
        payment.save(update_fields=["status", "raw_payload", "checkout_url", "updated_at"])
        raise ValidationError("Chapa transaction has not completed successfully.")

    if payment.status == PaymentStatus.PAID or payment.order.status == OrderStatus.PAID:
        payment.raw_payload = response
        payment.save(update_fields=["raw_payload", "updated_at"])
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=payment.order)
        return confirmation

    return mark_order_paid(payment.order, reference=tx_ref, payload=response)


def chapa_request(method, path, payload=None):
    base_url = settings.CHAPA_BASE_URL.rstrip("/")
    url = f"{base_url}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {settings.CHAPA_SECRET_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        try:
            error_payload = json.loads(error_body)
        except json.JSONDecodeError:
            error_payload = {"message": error_body}
        raise ValidationError(error_payload.get("message") or "Chapa request failed.") from exc
    except (URLError, TimeoutError) as exc:
        raise ValidationError("Unable to connect to Chapa. Please try again.") from exc


@transaction.atomic
def assign_driver(order, driver):
    if order.selected_agent_id != driver.agent_id:
        raise ValidationError("The selected driver must belong to the assigned agent.")
    if not driver.can_receive_assignments:
        raise ValidationError("Only available active drivers can be assigned to deliveries.")
    if order.status not in ACTIVE_DELIVERY_STATUSES | {OrderStatus.PAID}:
        raise ValidationError("A driver can only be assigned after payment and before delivery completion.")

    previous_driver_user = order.assigned_driver.user if order.assigned_driver and order.assigned_driver.user_id != driver.user_id else None
    now = timezone.now()
    order.assigned_driver = driver
    order.status = OrderStatus.DRIVER_ASSIGNED
    order.driver_assigned_at = order.driver_assigned_at or now
    order.driver_accepted_at = None
    order.picked_up_at = None
    order.out_for_delivery_at = None
    order.arrived_at = None
    order.failed_at = None
    order.save()

    if previous_driver_user:
        notify_user(
            previous_driver_user,
            "Delivery reassigned",
            f"Order {order.order_number} has been reassigned to another driver.",
            link=reverse("accounts:driver_dashboard"),
        )
    notify_user(
        driver.user,
        "Delivery assigned",
        f"You have been assigned to deliver order {order.order_number}.",
        link=reverse("accounts:driver_dashboard"),
    )
    notify_user(
        order.customer,
        "Driver assigned",
        f"A driver has been assigned to order {order.order_number}.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    send_order_status_email(
        order,
        "Driver assigned",
        "A driver has been assigned and your order is moving into fulfillment.",
    )
    return order


def _ensure_assigned_driver(order, driver_user):
    if not order.assigned_driver or order.assigned_driver.user_id != driver_user.id:
        raise ValidationError("You are not assigned to this delivery.")


@transaction.atomic
def accept_delivery_assignment(order, driver_user):
    _ensure_assigned_driver(order, driver_user)
    if order.status not in {OrderStatus.DRIVER_ASSIGNED, OrderStatus.DRIVER_ACCEPTED}:
        raise ValidationError("This delivery cannot be accepted in its current state.")
    if order.status == OrderStatus.DRIVER_ACCEPTED:
        return order

    if order.assigned_driver:
        order.assigned_driver.availability_status = order.assigned_driver.AvailabilityStatus.ON_DELIVERY
        order.assigned_driver.save(update_fields=["availability_status", "updated_at"])
    order.status = OrderStatus.DRIVER_ACCEPTED
    order.driver_accepted_at = timezone.now()
    order.save()
    notify_user(
        order.customer,
        "Driver accepted delivery",
        f"The assigned driver accepted {order.order_number} and is preparing for dispatch.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    send_order_status_email(
        order,
        "Driver accepted delivery",
        "Your assigned driver has accepted the delivery and is preparing the handoff.",
    )
    return order


@transaction.atomic
def mark_order_picked_up(order, driver_user):
    _ensure_assigned_driver(order, driver_user)
    if order.status not in {OrderStatus.DRIVER_ASSIGNED, OrderStatus.DRIVER_ACCEPTED, OrderStatus.PICKED_UP}:
        raise ValidationError("This delivery cannot be marked as picked up right now.")
    if order.status == OrderStatus.PICKED_UP:
        return order

    now = timezone.now()
    if order.assigned_driver:
        order.assigned_driver.availability_status = order.assigned_driver.AvailabilityStatus.ON_DELIVERY
        order.assigned_driver.save(update_fields=["availability_status", "updated_at"])
    if not order.driver_accepted_at:
        order.driver_accepted_at = now
    order.status = OrderStatus.PICKED_UP
    order.picked_up_at = now
    order.save()
    notify_user(
        order.customer,
        "Order picked up",
        f"Your order {order.order_number} has been picked up from the warehouse.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Order picked up",
            f"{order.order_number} was collected from the warehouse by {driver_user.full_name}.",
            link=reverse("accounts:agent_dashboard"),
        )
    send_order_status_email(
        order,
        "Order picked up",
        "The assigned driver has collected your order and is almost ready to start the route.",
    )
    return order


@transaction.atomic
def start_delivery(order, driver_user):
    _ensure_assigned_driver(order, driver_user)
    if order.status not in {
        OrderStatus.DRIVER_ASSIGNED,
        OrderStatus.DRIVER_ACCEPTED,
        OrderStatus.PICKED_UP,
        OrderStatus.OUT_FOR_DELIVERY,
    }:
        raise ValidationError("This delivery cannot be started in its current state.")
    if order.status == OrderStatus.OUT_FOR_DELIVERY:
        return order

    now = timezone.now()
    if order.assigned_driver:
        order.assigned_driver.availability_status = order.assigned_driver.AvailabilityStatus.ON_DELIVERY
        order.assigned_driver.save(update_fields=["availability_status", "updated_at"])
    if not order.driver_accepted_at:
        order.driver_accepted_at = now
    if not order.picked_up_at:
        order.picked_up_at = now
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.out_for_delivery_at = now
    order.save()
    notify_user(
        order.customer,
        "Delivery started",
        f"Your order {order.order_number} is now on the way.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    send_order_status_email(
        order,
        "Delivery started",
        "Your driver is on the way and live tracking is now active on the order page.",
    )
    return order


@transaction.atomic
def mark_order_arrived(order, driver_user):
    _ensure_assigned_driver(order, driver_user)
    if order.status not in {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.ARRIVED}:
        raise ValidationError("This delivery cannot be marked as arrived right now.")
    if order.status == OrderStatus.ARRIVED:
        return order

    if order.assigned_driver:
        order.assigned_driver.availability_status = order.assigned_driver.AvailabilityStatus.ON_DELIVERY
        order.assigned_driver.save(update_fields=["availability_status", "updated_at"])
    order.status = OrderStatus.ARRIVED
    order.arrived_at = timezone.now()
    order.save()
    notify_user(
        order.customer,
        "Driver arrived",
        f"Your driver has arrived for order {order.order_number}. Please open your QR code.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    send_order_status_email(
        order,
        "Driver arrived",
        "Your driver has arrived. Please open the QR code on your order page for confirmation.",
    )
    return order


@transaction.atomic
def report_delivery_issue(order, driver_user, issue_type, description=""):
    _ensure_assigned_driver(order, driver_user)
    if order.status in {OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.FAILED}:
        raise ValidationError("This delivery can no longer be flagged with a new issue.")

    issue = DeliveryIssue.objects.create(
        order=order,
        reported_by=driver_user,
        issue_type=issue_type,
        description=description,
    )
    _set_latest_status_note(
        order,
        f"Driver reported {issue.get_issue_type_display().lower()}. Awaiting agent manager review before any delivery failure decision.",
    )

    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Delivery issue reported",
            (
                f"{driver_user.full_name} reported {issue.get_issue_type_display().lower()} for "
                f"{order.order_number}. Manager approval is required before the order can be marked failed."
            ),
            link=reverse("accounts:agent_dashboard"),
        )
    notify_user(
        order.customer,
        "Delivery issue reported",
        f"There is an issue with {order.order_number}. The agent manager has been notified and is reviewing it.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    send_order_status_email(
        order,
        "Delivery issue reported",
        (
            f"We recorded a delivery issue for your order: {issue.get_issue_type_display()}. "
            "The agent manager will review it before any failure decision is made."
        ),
    )
    return issue


@transaction.atomic
def submit_delivery_feedback(order, customer_user, rating, comment="", photo=None):
    if order.customer_id != customer_user.id:
        raise ValidationError("You can only submit feedback for your own order.")
    if order.status != OrderStatus.DELIVERED:
        raise ValidationError("Feedback is only available after delivery is complete.")

    feedback, _ = DeliveryFeedback.objects.get_or_create(
        order=order,
        defaults={
            "customer": customer_user,
            "driver": order.assigned_driver,
        },
    )
    feedback.customer = customer_user
    feedback.driver = order.assigned_driver
    feedback.rating = rating
    feedback.comment = comment
    feedback.skipped_at = None
    if photo is not None:
        feedback.photo = photo
    feedback.full_clean()
    feedback.save()

    if order.assigned_driver and order.assigned_driver.user_id:
        notify_user(
            order.assigned_driver.user,
            "New delivery rating",
            f"You received a {rating}-star rating for {order.order_number}.",
            link=reverse("accounts:driver_dashboard"),
        )
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Customer feedback submitted",
            f"{order.customer.full_name} left feedback for {order.order_number}.",
            link=reverse("accounts:agent_dashboard"),
        )
    return feedback


@transaction.atomic
def skip_delivery_feedback(order, customer_user):
    if order.customer_id != customer_user.id:
        raise ValidationError("You can only skip feedback for your own order.")
    if order.status != OrderStatus.DELIVERED:
        raise ValidationError("Feedback is only available after delivery is complete.")

    feedback, _ = DeliveryFeedback.objects.get_or_create(
        order=order,
        defaults={
            "customer": customer_user,
            "driver": order.assigned_driver,
        },
    )
    feedback.customer = customer_user
    feedback.driver = order.assigned_driver
    feedback.rating = None
    feedback.comment = ""
    feedback.photo = None
    feedback.skipped_at = timezone.now()
    feedback.full_clean()
    feedback.save()
    return feedback


def _parse_qr_payload(raw_qr_data):
    try:
        payload = json.loads((raw_qr_data or "").strip())
    except json.JSONDecodeError as exc:
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        ) from exc
    if not isinstance(payload, dict):
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        )
    required_fields = {"order_id", "customer_id", "token", "expires_at"}
    if not required_fields.issubset(payload.keys()):
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        )
    return payload


def _validate_confirmation_state(order, confirmation):
    if order.status == OrderStatus.DELIVERED or confirmation.scanned_at:
        raise QRConfirmationError("ALREADY_SCANNED", "This delivery has already been confirmed.")


@transaction.atomic
def _mark_order_delivered(order, confirmation, verified_by_user):
    deduct_inventory_fefo(order)
    now = timezone.now()
    confirmation.scanned_at = now
    confirmation.scanned_by = verified_by_user
    confirmation.verified_at = now
    confirmation.verified_by = verified_by_user
    confirmation.save(update_fields=["scanned_at", "scanned_by", "verified_at", "verified_by", "updated_at"])

    order.status = OrderStatus.DELIVERED
    order.delivered_at = now
    if not order.arrived_at:
        order.arrived_at = now
    order.save()
    if order.assigned_driver:
        order.assigned_driver.availability_status = order.assigned_driver.AvailabilityStatus.AVAILABLE
        order.assigned_driver.save(update_fields=["availability_status", "updated_at"])

    notify_user(
        order.customer,
        "Order delivered",
        f"Order {order.order_number} has been delivered successfully.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Delivery completed",
            f"{order.order_number} was delivered successfully.",
            link=reverse("accounts:agent_dashboard"),
        )
    send_order_status_email(
        order,
        "Delivery complete",
        "Your order was confirmed successfully and has been marked as delivered.",
    )
    return order


@transaction.atomic
def confirm_delivery_by_qr_token(order, qr_token, driver_user, payload_metadata=None):
    _ensure_assigned_driver(order, driver_user)
    confirmation = getattr(order, "confirmation", None)
    if not confirmation:
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        )
    if order.status not in QR_CONFIRMABLE_STATUSES:
        raise ValidationError("The driver must mark the delivery as arrived before confirming the QR code.")
    _validate_confirmation_state(order, confirmation)

    token = (qr_token or "").strip()
    if token != confirmation.qr_token:
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        )

    try:
        decoded_token = decode_signed_qr_token(token)
    except QRTokenError as exc:
        raise QRConfirmationError(
            "INVALID_TOKEN",
            "This QR code is not valid. Ask the customer to refresh their order page.",
        ) from exc

    expected_customer_id = build_customer_token_id(order.customer_id)
    if decoded_token.get("order_id") != order.order_number or decoded_token.get("customer_id") != expected_customer_id:
        raise QRConfirmationError("ORDER_MISMATCH", "This QR code belongs to a different order.")

    expires_at = confirmation.expires_at
    if payload_metadata:
        if payload_metadata.get("order_id") != order.order_number or payload_metadata.get("customer_id") != expected_customer_id:
            raise QRConfirmationError("ORDER_MISMATCH", "This QR code belongs to a different order.")
        try:
            expires_at = timezone.datetime.fromisoformat(payload_metadata.get("expires_at", ""))
        except ValueError as exc:
            raise QRConfirmationError(
                "INVALID_TOKEN",
                "This QR code is not valid. Ask the customer to refresh their order page.",
            ) from exc
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())

    token_expiry = timezone.datetime.fromtimestamp(int(decoded_token["exp"]), tz=dt_timezone.utc)
    now = timezone.now()
    if (expires_at and now >= expires_at) or now >= token_expiry or confirmation.is_expired:
        raise QRConfirmationError("EXPIRED", "This QR code has expired. The customer needs to request a new one.")

    return _mark_order_delivered(order, confirmation, driver_user)


@transaction.atomic
def confirm_delivery_by_qr(order, raw_qr_data, driver_user):
    payload = _parse_qr_payload(raw_qr_data)
    return confirm_delivery_by_qr_token(order, payload.get("token"), driver_user, payload_metadata=payload)


def submit_queued_qr_scans(scan_items, driver_user):
    results = []
    for item in scan_items:
        order_number = (item.get("order_id") or item.get("delivery_id") or "").strip()
        raw_payload = item.get("qr_payload", "")
        try:
            order = Order.objects.select_related("confirmation", "assigned_driver__user", "selected_agent").get(
                order_number=order_number
            )
            confirm_delivery_by_qr(order, raw_payload, driver_user)
            results.append({"order_id": order_number, "status": "confirmed"})
        except QRConfirmationError as exc:
            results.append({"order_id": order_number, "status": "failed", "error": exc.code, "message": exc.message})
        except (ValidationError, Order.DoesNotExist) as exc:
            message = str(exc)
            results.append({"order_id": order_number, "status": "failed", "error": "INVALID_STATE", "message": message})
    return results


@transaction.atomic
def complete_delivery_and_deduct_stock(order, otp_code, qr_token, verified_by_user):
    confirmation = order.confirmation
    if not confirmation:
        raise ValidationError("This order does not have a delivery confirmation record.")
    submitted_otp = (otp_code or "").strip()
    submitted_qr_token = (qr_token or "").strip()
    if submitted_qr_token.startswith("{"):
        return confirm_delivery_by_qr(order, submitted_qr_token, verified_by_user)
    if "|" in submitted_qr_token:
        payload_parts = [part.strip() for part in submitted_qr_token.split("|")]
        if len(payload_parts) >= 3:
            payload_order_number, payload_qr_token, payload_otp = payload_parts[:3]
            if payload_order_number and payload_order_number != order.order_number:
                raise ValidationError("This QR code belongs to a different order.")
            submitted_qr_token = payload_qr_token
            if not submitted_otp:
                submitted_otp = payload_otp
    if not submitted_otp and not submitted_qr_token:
        raise ValidationError("Enter either the OTP code or the QR code/token to complete delivery.")

    if submitted_qr_token:
        try:
            return confirm_delivery_by_qr_token(order, submitted_qr_token, verified_by_user)
        except QRConfirmationError as exc:
            raise ValidationError(exc.message) from exc

    _ensure_assigned_driver(order, verified_by_user)
    if order.status not in QR_CONFIRMABLE_STATUSES:
        raise ValidationError("The driver must mark the delivery as arrived before completing handoff.")
    _validate_confirmation_state(order, confirmation)
    if confirmation.otp_code != submitted_otp:
        raise ValidationError("The OTP code does not match this order.")
    return _mark_order_delivered(order, confirmation, verified_by_user)


@transaction.atomic
def cancel_order(order, requested_by, reason=""):
    if order.status == OrderStatus.CANCELLED:
        raise ValidationError("This order has already been cancelled.")
    if order.status in {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, OrderStatus.REJECTED}:
        raise ValidationError("This order can no longer be cancelled from the customer portal.")
    if order.refund_requests.filter(
        request_type=RefundRequestType.CANCELLATION,
        status=RefundRequestStatus.APPROVED,
    ).exists():
        raise ValidationError("A cancellation has already been processed for this order.")

    payment = getattr(order, "payment", None)
    paid_statuses = {PaymentStatus.PAID, PaymentStatus.PARTIALLY_REFUNDED}
    is_paid = bool(payment and payment.status in paid_statuses)
    fee_percent = Decimal("0.00")
    fee_amount = Decimal("0.00")
    approved_amount = Decimal("0.00")

    if is_paid:
        if not order.cancellation_window_open:
            raise ValidationError("The cancellation window has passed for this paid order.")
        fee_percent = Decimal(settings.ORDER_CANCELLATION_FEE_PERCENT)
        fee_amount = quantize_money(payment.amount * fee_percent / Decimal("100"))
        approved_amount = quantize_money(payment.amount - fee_amount)
        payment.status = PaymentStatus.PARTIALLY_REFUNDED if approved_amount < payment.amount else PaymentStatus.REFUNDED
        payment.raw_payload = {
            **(payment.raw_payload or {}),
            "cancellation_refund": {
                "fee_percent": str(fee_percent),
                "fee_amount": str(fee_amount),
                "approved_amount": str(approved_amount),
                "processed_at": timezone.now().isoformat(),
                "payout_method": RefundPayoutMethod.GATEWAY,
            },
        }
        payment.save(update_fields=["status", "raw_payload", "updated_at"])
    elif payment and payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.CANCELLED
        payment.checkout_url = ""
        payment.save(update_fields=["status", "checkout_url", "updated_at"])

    release_reserved_stock(order)
    order.status = OrderStatus.CANCELLED
    order.assigned_driver = None
    order.save()
    cancellation_note = "Customer cancelled the order."
    if is_paid:
        cancellation_note = (
            f"Customer cancelled within the refund window. Refunded {approved_amount} after a {fee_percent}% cancellation fee."
        )
    _set_latest_status_note(order, cancellation_note)

    refund_request = RefundRequest.objects.create(
        order=order,
        requested_by=requested_by,
        request_type=RefundRequestType.CANCELLATION,
        status=RefundRequestStatus.PROCESSED,
        payout_method=RefundPayoutMethod.GATEWAY,
        reason=reason,
        requested_amount=payment.amount if payment else Decimal("0.00"),
        fee_percent=fee_percent,
        fee_amount=fee_amount,
        approved_amount=approved_amount,
        reviewed_by=requested_by,
        reviewed_at=timezone.now(),
        processed_at=timezone.now(),
        processed_by=requested_by,
        resolution_note=cancellation_note,
    )

    notify_user(
        order.customer,
        "Order cancelled",
        f"{order.order_number} was cancelled successfully.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Order cancelled",
            f"{order.order_number} was cancelled by the customer.",
            link=reverse("accounts:agent_dashboard"),
        )
    send_cancellation_email(order, refund_request)
    return refund_request


@transaction.atomic
def request_order_refund(order, requested_by, reason, payout_method=RefundPayoutMethod.GATEWAY, photos=None):
    if not order.can_request_refund:
        raise ValidationError("This order is not eligible for a service refund request anymore.")
    if order.refund_requests.filter(
        request_type=RefundRequestType.SERVICE_ISSUE,
    ).exists():
        raise ValidationError("A service refund request already exists for this order.")

    payment = getattr(order, "payment", None)
    requested_amount = payment.amount if payment else order.total
    refund_request = RefundRequest.objects.create(
        order=order,
        requested_by=requested_by,
        request_type=RefundRequestType.SERVICE_ISSUE,
        payout_method=payout_method,
        reason=reason,
        requested_amount=requested_amount,
    )
    for photo in photos or []:
        RefundEvidence.objects.create(refund_request=refund_request, image=photo)
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "New refund request",
            f"{order.order_number} has a new service refund request waiting for review.",
            link=reverse("accounts:agent_refunds"),
        )
    company_admins = list(get_company_admin_users(order.company))
    if company_admins:
        selected_agent_admin_id = getattr(order.selected_agent, "admin_id", None)
        for admin_user in company_admins:
            if not order.selected_agent or not selected_agent_admin_id or admin_user.pk != selected_agent_admin_id:
                notify_user(
                    admin_user,
                    "New refund request",
                    f"{order.order_number} has a new service refund request waiting for review.",
                    link=reverse("accounts:company_dashboard"),
                )
    notify_user(
        order.customer,
        "Refund request received",
        f"We received your refund request for {order.order_number}. The company will review it shortly.",
        link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
    )
    send_refund_request_email(order, refund_request)
    return refund_request


@transaction.atomic
def approve_refund_request(refund_request, reviewed_by, approved_amount=None, resolution_note=""):
    if refund_request.status != RefundRequestStatus.PENDING:
        raise ValidationError("This refund request has already been reviewed.")
    resolution_note = (resolution_note or "").strip()
    if not resolution_note:
        raise ValidationError("A written reason is required when approving a refund.")

    payment = getattr(refund_request.order, "payment", None)
    if payment is None:
        raise ValidationError("This order does not have a payment to refund.")

    approved_amount_value = refund_request.requested_amount if approved_amount in (None, "") else approved_amount
    approved_amount = quantize_money(approved_amount_value)
    if approved_amount <= 0:
        raise ValidationError("Approved refund amount must be greater than zero.")
    if approved_amount > payment.amount:
        raise ValidationError("Approved refund amount cannot be greater than the paid amount.")

    refund_request.status = RefundRequestStatus.APPROVED
    refund_request.approved_amount = approved_amount
    refund_request.reviewed_by = reviewed_by
    refund_request.reviewed_at = timezone.now()
    refund_request.resolution_note = resolution_note
    refund_request.failure_reason = ""
    refund_request.save(update_fields=[
        "status",
        "approved_amount",
        "reviewed_by",
        "reviewed_at",
        "resolution_note",
        "failure_reason",
        "updated_at",
    ])

    OrderStatusHistory.objects.create(
        order=refund_request.order,
        status=refund_request.order.status,
        note=f"Refund approved for {approved_amount}. {resolution_note}".strip(),
    )
    notify_user(
        refund_request.order.customer,
        "Refund approved",
        f"Your refund for {refund_request.order.order_number} was approved for {approved_amount} and is waiting to be processed.",
        link=reverse("orders:detail", kwargs={"order_number": refund_request.order.order_number}),
    )
    send_refund_resolution_email(refund_request.order, refund_request, approved=True)
    return refund_request


@transaction.atomic
def reject_refund_request(refund_request, reviewed_by, resolution_note=""):
    if refund_request.status != RefundRequestStatus.PENDING:
        raise ValidationError("This refund request has already been reviewed.")
    resolution_note = (resolution_note or "").strip()
    if not resolution_note:
        raise ValidationError("A written reason is required when rejecting a refund.")

    refund_request.status = RefundRequestStatus.REJECTED
    refund_request.reviewed_by = reviewed_by
    refund_request.reviewed_at = timezone.now()
    refund_request.resolution_note = resolution_note
    refund_request.failure_reason = ""
    refund_request.save(update_fields=[
        "status",
        "reviewed_by",
        "reviewed_at",
        "resolution_note",
        "failure_reason",
        "updated_at",
    ])

    OrderStatusHistory.objects.create(
        order=refund_request.order,
        status=refund_request.order.status,
        note=f"Refund rejected. {resolution_note}".strip(),
    )
    notify_user(
        refund_request.order.customer,
        "Refund update",
        f"Your refund request for {refund_request.order.order_number} was not approved.",
        link=reverse("orders:detail", kwargs={"order_number": refund_request.order.order_number}),
    )
    send_refund_resolution_email(refund_request.order, refund_request, approved=False)
    return refund_request


@transaction.atomic
def process_refund_request(refund_request, processed_by, failure_reason=""):
    if refund_request.status not in {RefundRequestStatus.APPROVED, RefundRequestStatus.FAILED}:
        raise ValidationError("Only approved refund requests can be processed.")

    failure_reason = (failure_reason or "").strip()
    payment = getattr(refund_request.order, "payment", None)
    if payment is None:
        raise ValidationError("This order does not have a payment to refund.")

    if failure_reason:
        refund_request.status = RefundRequestStatus.FAILED
        refund_request.processed_by = processed_by
        refund_request.processed_at = timezone.now()
        refund_request.failure_reason = failure_reason
        refund_request.save(update_fields=["status", "processed_by", "processed_at", "failure_reason", "updated_at"])
        notify_user(
            refund_request.order.customer,
            "Refund processing failed",
            f"We were unable to process the refund for {refund_request.order.order_number} yet.",
            link=reverse("orders:detail", kwargs={"order_number": refund_request.order.order_number}),
        )
        return refund_request

    approved_amount = refund_request.approved_amount or refund_request.requested_amount
    customer = refund_request.order.customer
    if refund_request.payout_method == RefundPayoutMethod.WALLET_CREDIT and customer:
        customer.credit_wallet(approved_amount)
    payment.status = PaymentStatus.REFUNDED if approved_amount >= payment.amount else PaymentStatus.PARTIALLY_REFUNDED
    payment.raw_payload = {
        **(payment.raw_payload or {}),
        "service_refund": {
            "approved_amount": str(approved_amount),
            "reviewed_by": refund_request.reviewed_by.email if refund_request.reviewed_by else "",
            "processed_by": processed_by.email,
            "processed_at": timezone.now().isoformat(),
            "note": refund_request.resolution_note,
            "payout_method": refund_request.payout_method,
        },
    }
    payment.save(update_fields=["status", "raw_payload", "updated_at"])

    refund_request.status = RefundRequestStatus.PROCESSED
    refund_request.processed_by = processed_by
    refund_request.processed_at = timezone.now()
    refund_request.failure_reason = ""
    refund_request.save(update_fields=["status", "processed_by", "processed_at", "failure_reason", "updated_at"])

    OrderStatusHistory.objects.create(
        order=refund_request.order,
        status=refund_request.order.status,
        note=f"Refund processed for {approved_amount} via {refund_request.get_payout_method_display().lower()}.",
    )
    notify_user(
        refund_request.order.customer,
        "Refund processed",
        f"Your refund for {refund_request.order.order_number} has been processed via {refund_request.get_payout_method_display().lower()}.",
        link=reverse("orders:detail", kwargs={"order_number": refund_request.order.order_number}),
    )
    return refund_request


def send_delivery_confirmation_email(order, confirmation):
    item_lines = "\n".join(
        f"- {item.product_name} x {item.quantity} @ {item.unit_price} = {item.line_total}"
        for item in order.items.all()
    )
    agent_name = order.selected_agent.name if order.selected_agent else "Pending assignment"
    discount_line = ""
    if order.discount_amount > 0:
        discount_line = (
            f"Premium discount: -{order.discount_amount} "
            f"({order.premium_discount_percent}% streak reward)\n"
        )
    send_mail(
        subject=f"Payment confirmed for {order.order_number}",
        message=(
            f"Hello {order.customer.first_name},\n\n"
            f"Your payment for order {order.order_number} has been confirmed.\n\n"
            f"Company: {order.company.name}\n"
            f"Agent branch: {agent_name}\n"
            f"Delivery address: {order.delivery_address}\n"
            f"Location coordinates: {order.latitude}, {order.longitude}\n"
            f"Phone number: {order.phone_number}\n\n"
            "Order details:\n"
            f"{item_lines}\n\n"
            f"Subtotal: {order.subtotal}\n"
            f"{discount_line}"
            f"Delivery fee: {order.delivery_fee}\n"
            f"Total paid: {order.total}\n\n"
            f"Confirmation code: {confirmation.otp_code}\n"
            f"QR expires at: {timezone.localtime(confirmation.expires_at).strftime('%Y-%m-%d %H:%M')}\n\n"
            "When the driver arrives, open your order page so the driver can scan your delivery code. "
            "If the QR expires before delivery, request a fresh one from the order page."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[order.customer.email],
        fail_silently=True,
    )


def send_order_status_email(order, subject_line, body):
    send_mail(
        subject=f"{subject_line} - {order.order_number}",
        message=body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[order.customer.email],
        fail_silently=True,
    )


def send_cancellation_email(order, refund_request):
    if refund_request.approved_amount > 0:
        summary = (
            f"Refund approved: {refund_request.approved_amount}\n"
            f"Cancellation fee deducted: {refund_request.fee_amount} ({refund_request.fee_percent}%)\n"
        )
    else:
        summary = "No payment refund was needed for this cancellation.\n"

    send_mail(
        subject=f"Order cancellation for {order.order_number}",
        message=(
            f"Your order {order.order_number} was cancelled successfully.\n\n"
            f"{summary}"
            "If you still need water delivery, you can place a new order at any time."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[order.customer.email],
        fail_silently=True,
    )


def send_refund_request_email(order, refund_request):
    send_mail(
        subject=f"Refund request received for {order.order_number}",
        message=(
            f"We received your refund request for {order.order_number}.\n\n"
            f"Reason submitted:\n{refund_request.reason}\n\n"
            "The company will review this request and update you once a decision is made."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[order.customer.email],
        fail_silently=True,
    )


def send_refund_resolution_email(order, refund_request, approved):
    if approved:
        message = (
            f"Your refund request for {order.order_number} was approved.\n\n"
            f"Approved amount: {refund_request.approved_amount}\n"
        )
    else:
        message = f"Your refund request for {order.order_number} was not approved.\n\n"
    if refund_request.resolution_note:
        message += f"Review note: {refund_request.resolution_note}\n"

    send_mail(
        subject=f"Refund update for {order.order_number}",
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@water.local"),
        recipient_list=[order.customer.email],
        fail_silently=True,
    )


def deduct_inventory_fefo(order):
    if not order.selected_agent:
        raise ValidationError("Order does not have an assigned agent.")

    for item in order.items.select_related("product"):
        stock = AgentStock.objects.select_for_update().get(agent=order.selected_agent, product=item.product)
        active_reserved_quantity = (
            OrderItem.objects.filter(
                order__selected_agent=order.selected_agent,
                product=item.product,
                order__status__in=RESERVED_STOCK_ORDER_STATUSES,
            )
            .exclude(order=order)
            .aggregate(total=Sum("quantity"))["total"]
            or 0
        )
        remaining = item.quantity
        batches = _sync_inventory_batches_for_item(order.selected_agent, item, stock, active_reserved_quantity)
        total_available = sum(batch.quantity_remaining for batch in batches)
        if total_available < item.quantity:
            raise ValidationError(f"Not enough FEFO batch stock available for {item.product_name}.")

        for batch in batches:
            if remaining <= 0:
                break
            deduction = min(batch.quantity_remaining, remaining)
            batch.quantity_remaining -= deduction
            batch.save(update_fields=["quantity_remaining", "updated_at"])
            remaining -= deduction

        batch_available_quantity = sum(batch.quantity_remaining for batch in batches)
        stock.available_quantity = max(0, batch_available_quantity - active_reserved_quantity)
        stock.save(update_fields=["available_quantity", "updated_at"])
        create_inventory_transaction(
            agent=order.selected_agent,
            product=item.product,
            transaction_type=InventoryTransactionType.SALE,
            quantity_change=-item.quantity,
            stock_after=stock.available_quantity,
            performed_by=getattr(getattr(order, "assigned_driver", None), "user", None),
            reference=order.order_number,
            note=f"Stock consumed for delivered order {order.order_number}.",
        )


def generate_payment_reference(order):
    return f"CHAPA-{order.order_number}-{uuid.uuid4().hex[:8].upper()}"
