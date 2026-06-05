import uuid
import json
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.services import notify_user
from core.policies import get_cart_pricing_summary, quantize_money
from cart.services import get_or_create_cart
from catalog.models import Agent, AgentStock, InventoryBatch
from orders.models import (
    AgentRequestStatus,
    DeliveryConfirmation,
    Order,
    OrderAgentRequest,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    RefundRequest,
    RefundRequestStatus,
    RefundRequestType,
    OrderStatusHistory,
)


def get_cart_company(cart):
    first_item = cart.items.select_related("product__company").first()
    return first_item.product.company if first_item else None


def get_eligible_agents(company, items, latitude, longitude):
    eligible_agents = []
    for option in get_agent_delivery_options(company, items, latitude, longitude):
        if option["is_eligible"]:
            eligible_agents.append((option["agent"], option["distance_km"]))
    return eligible_agents


def get_agent_delivery_options(company, items, latitude, longitude):
    options = []
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


def release_reserved_stock(order):
    if not order.selected_agent_id:
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
    now = timezone.now()
    pricing_summary = get_cart_pricing_summary(cart)
    _reserve_stock_for_items(selected_agent, items)

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
        status=OrderStatus.PAYMENT_PENDING,
        accepted_at=now,
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
        status=AgentRequestStatus.ACCEPTED,
        distance_km=selected_distance,
        note="Automatically selected during checkout.",
        responded_at=now,
    )

    Payment.objects.update_or_create(
        order=order,
        defaults={
            "provider": PaymentProvider.CHAPA,
            "status": PaymentStatus.PENDING,
            "amount": order.total,
            "reference": generate_payment_reference(order),
        },
    )

    cart.items.all().delete()
    notify_user(
        user,
        "Order ready for payment",
        f"{order.order_number} has been prepared with {selected_agent.name}. Complete payment to confirm delivery.",
        link=reverse("orders:payment", kwargs={"order_number": order.order_number}),
    )
    return order


@transaction.atomic
def accept_agent_request(agent_request, note="", accepted_by=None):
    order = agent_request.order
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
    order.rejection_reason = ""
    order.save()
    if order.customer:
        notify_user(
            order.customer,
            "Order accepted",
            f"{order.order_number} was accepted. You can proceed to payment now.",
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
    order = agent_request.order
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
        order.rejection_reason = note or "All nearby agents declined the request."
        order.save()
        if order.customer:
            notify_user(
                order.customer,
                "Order rejected",
                f"{order.order_number} could not be accepted by nearby agents.",
                link=reverse("orders:detail", kwargs={"order_number": order.order_number}),
            )

    return order


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
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
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

    confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
    send_delivery_confirmation_email(order, confirmation)
    if order.selected_agent and order.selected_agent.admin:
        notify_user(
            order.selected_agent.admin,
            "Payment confirmed",
            f"{order.order_number} has been paid and is ready for driver assignment.",
            link=reverse("accounts:agent_dashboard"),
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
    order.assigned_driver = driver
    order.status = OrderStatus.DRIVER_ASSIGNED
    order.driver_assigned_at = timezone.now()
    order.save()
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
    return order


@transaction.atomic
def start_delivery(order, driver_user):
    if not order.assigned_driver or order.assigned_driver.user_id != driver_user.id:
        raise ValidationError("You are not assigned to this delivery.")
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.out_for_delivery_at = timezone.now()
    order.save()
    notify_user(
        order.customer,
        "Delivery started",
        f"Your order {order.order_number} is now on the way.",
        link=reverse("orders:tracking", kwargs={"order_number": order.order_number}),
    )
    return order


@transaction.atomic
def complete_delivery_and_deduct_stock(order, otp_code, qr_token, verified_by_user):
    confirmation = order.confirmation
    if not confirmation:
        raise ValidationError("This order does not have a delivery confirmation record.")
    submitted_otp = (otp_code or "").strip()
    submitted_qr_token = (qr_token or "").strip()
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

    otp_matches = bool(submitted_otp) and confirmation.otp_code == submitted_otp
    qr_matches = bool(submitted_qr_token) and confirmation.qr_token == submitted_qr_token
    if not (otp_matches or qr_matches):
        raise ValidationError("The OTP code or QR token does not match this order.")

    deduct_inventory_fefo(order)
    confirmation.verified_at = timezone.now()
    confirmation.verified_by = verified_by_user
    confirmation.save(update_fields=["verified_at", "verified_by", "updated_at"])

    order.status = OrderStatus.DELIVERED
    order.delivered_at = timezone.now()
    order.save()

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
    return order


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
        status=RefundRequestStatus.APPROVED,
        reason=reason,
        requested_amount=payment.amount if payment else Decimal("0.00"),
        fee_percent=fee_percent,
        fee_amount=fee_amount,
        approved_amount=approved_amount,
        reviewed_by=requested_by,
        reviewed_at=timezone.now(),
        processed_at=timezone.now(),
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
def request_order_refund(order, requested_by, reason):
    if not order.can_request_refund:
        raise ValidationError("This order is not eligible for a service refund request anymore.")
    if order.refund_requests.filter(
        request_type=RefundRequestType.SERVICE_ISSUE,
        status=RefundRequestStatus.PENDING,
    ).exists():
        raise ValidationError("A service refund request is already pending review for this order.")

    payment = getattr(order, "payment", None)
    requested_amount = payment.amount if payment else order.total
    refund_request = RefundRequest.objects.create(
        order=order,
        requested_by=requested_by,
        request_type=RefundRequestType.SERVICE_ISSUE,
        reason=reason,
        requested_amount=requested_amount,
    )
    if order.company.admin:
        notify_user(
            order.company.admin,
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
    refund_request.processed_at = timezone.now()
    refund_request.resolution_note = resolution_note
    refund_request.save()

    payment.status = PaymentStatus.REFUNDED if approved_amount >= payment.amount else PaymentStatus.PARTIALLY_REFUNDED
    payment.raw_payload = {
        **(payment.raw_payload or {}),
        "service_refund": {
            "approved_amount": str(approved_amount),
            "reviewed_by": reviewed_by.email,
            "processed_at": refund_request.processed_at.isoformat(),
            "note": resolution_note,
        },
    }
    payment.save(update_fields=["status", "raw_payload", "updated_at"])

    OrderStatusHistory.objects.create(
        order=refund_request.order,
        status=refund_request.order.status,
        note=f"Refund approved for {approved_amount}. {resolution_note}".strip(),
    )
    notify_user(
        refund_request.order.customer,
        "Refund approved",
        f"Your refund for {refund_request.order.order_number} was approved for {approved_amount}.",
        link=reverse("orders:detail", kwargs={"order_number": refund_request.order.order_number}),
    )
    send_refund_resolution_email(refund_request.order, refund_request, approved=True)
    return refund_request


@transaction.atomic
def reject_refund_request(refund_request, reviewed_by, resolution_note=""):
    if refund_request.status != RefundRequestStatus.PENDING:
        raise ValidationError("This refund request has already been reviewed.")

    refund_request.status = RefundRequestStatus.REJECTED
    refund_request.reviewed_by = reviewed_by
    refund_request.reviewed_at = timezone.now()
    refund_request.resolution_note = resolution_note
    refund_request.save()

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


def send_delivery_confirmation_email(order, confirmation):
    send_mail(
        subject=f"Delivery confirmation for {order.order_number}",
        message=(
            f"Your payment for {order.order_number} is confirmed.\n\n"
            f"OTP: {confirmation.otp_code}\n"
            f"QR Token: {confirmation.qr_token}\n\n"
            "Share this OTP or QR token with the assigned driver when your order arrives."
        ),
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
                order__status__in=[
                    OrderStatus.PAYMENT_PENDING,
                    OrderStatus.PAID,
                    OrderStatus.DRIVER_ASSIGNED,
                    OrderStatus.OUT_FOR_DELIVERY,
                ],
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


def generate_payment_reference(order):
    return f"CHAPA-{order.order_number}-{uuid.uuid4().hex[:8].upper()}"
