import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core import mail
from django.utils import timezone
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import CustomerAddress, User, UserRole
from cart.services import add_product_to_cart
from catalog.models import Agent, AgentStock, Company, CompanyRefundPolicyTier, Driver, InventoryBatch, InventoryTransaction, InventoryTransactionType, Product
from core.models import DriverLocation
from orders.models import (
    AgentRequestStatus,
    ComplaintCategory,
    ComplaintResolutionType,
    ComplaintStatus,
    ComplaintStatusHistory,
    DeliveryConfirmation,
    DeliveryFeedback,
    Order,
    OrderAgentRequest,
    OrderStatus,
    PaymentStatus,
    RefundPayoutMethod,
    RefundRequestStatusHistory,
    RefundRequestStatus,
    SupportActionLog,
)
from orders.services import (
    accept_agent_request,
    accept_delivery_assignment,
    approve_refund_request,
    assign_driver,
    cancel_order,
    complete_delivery_and_deduct_stock,
    mark_order_arrived,
    mark_order_picked_up,
    mark_order_paid,
    process_refund_request,
    reject_agent_request,
    request_order_refund,
    start_delivery,
)


class OrderFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="orders@example.com",
            password="StrongPass123!",
            first_name="Bini",
            last_name="Wave",
            phone_number="+251911000020",
            is_active=True,
        )
        self.agent_manager = User.objects.create_user(
            email="agent-manager@example.com",
            password="StrongPass123!",
            first_name="Agent",
            last_name="Manager",
            phone_number="+251911000021",
            role=UserRole.AGENT_MANAGER,
        )
        self.driver_user = User.objects.create_user(
            email="driver@example.com",
            password="StrongPass123!",
            first_name="Dawit",
            last_name="Driver",
            phone_number="+251911000022",
            role=UserRole.DRIVER,
        )
        self.client.force_login(self.user)
        self.company = Company.objects.create(
            name="Fresh River",
            description="Delivery company",
            location="Bahir Dar",
            is_verified=True,
            refunds_enabled=True,
            maximum_cancellation_period_minutes=120,
        )
        self.company_admin = User.objects.create_user(
            email="company-admin@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Admin",
            phone_number="+251911000023",
            role=UserRole.COMPANY_ADMIN,
            managed_company=self.company,
        )
        self.company.admin = self.company_admin
        self.company.save(update_fields=["admin", "updated_at"])
        self.refund_policy_tier = CompanyRefundPolicyTier.objects.create(
            company=self.company,
            start_minutes=0,
            end_minutes=120,
            refund_percent="80.00",
        )
        self.agent = Agent.objects.create(
            company=self.company,
            name="Fresh River Piassa Agent",
            location_name="Piassa",
            latitude="9.030000",
            longitude="38.740000",
            service_radius_km="20.00",
            is_active=True,
            is_accepting_orders=True,
            admin=self.agent_manager,
        )
        self.driver = Driver.objects.create(
            agent=self.agent,
            user=self.driver_user,
            vehicle_identifier="AA-12345",
            phone_number="+251911000022",
            is_active=True,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="Bulk Pack",
            description="Bulk delivery water pack",
            price="18.00",
            available_quantity=10,
        )
        self.agent_stock = AgentStock.objects.create(
            agent=self.agent,
            product=self.product,
            available_quantity=6,
            reorder_level=2,
        )
        self.inventory_batch = InventoryBatch.objects.create(
            agent=self.agent,
            product=self.product,
            batch_number="BATCH-001",
            quantity_received=6,
            quantity_remaining=6,
            base_unit_cost="12.00",
            expires_at="2026-12-31",
            received_at="2026-06-01",
        )

    @staticmethod
    def checkout_payment_stub():
        return type("PaymentStub", (), {"checkout_url": "https://checkout.chapa.co/pay/test-session"})()

    def create_checkout_order(self, quantity=1, delivery_address="Piassa", notes=""):
        add_product_to_cart(self.user, self.product, quantity)
        response = self.client.post(
            reverse("orders:checkout"),
            {
                "location_source": "current",
                "selected_agent_id": str(self.agent.pk),
                "delivery_address": delivery_address,
                "latitude": "9.031000",
                "longitude": "38.741000",
                "phone_number": "+251911000020",
                "notes": notes,
            },
        )
        return response, Order.objects.order_by("-created_at").first()

    def accept_checkout_order(self, order, note="Accepted for payment"):
        agent_request = order.agent_requests.get()
        accept_agent_request(agent_request, note=note, accepted_by=self.agent_manager)
        order.refresh_from_db()
        return order

    def deliver_existing_order(self, order):
        self.accept_checkout_order(order)
        mark_order_paid(order, reference=f"TX-{order.order_number}", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)
        complete_delivery_and_deduct_stock(order, "", order.confirmation.qr_payload_json, self.driver_user)
        order.refresh_from_db()
        return order

    def create_delivered_order(self, quantity=1, delivery_address="Piassa"):
        _, order = self.create_checkout_order(quantity=quantity, delivery_address=delivery_address)
        return self.deliver_existing_order(order)

    def test_checkout_creates_pending_agent_request_before_payment(self):
        response, order = self.create_checkout_order(quantity=2, delivery_address="Bole Road, Addis Ababa", notes="Leave at reception")

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        self.assertEqual(order.company, self.company)
        self.assertEqual(order.status, OrderStatus.REQUESTED)
        self.assertEqual(order.selected_agent, self.agent)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.agent_requests.count(), 1)
        self.assertEqual(order.agent_requests.get().status, AgentRequestStatus.PENDING)
        self.assertFalse(hasattr(order, "payment"))
        self.agent_stock.refresh_from_db()
        self.assertEqual(self.agent_stock.available_quantity, 6)

    def test_checkout_page_loads_successfully(self):
        add_product_to_cart(self.user, self.product, 1)
        response = self.client.get(reverse("orders:checkout"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose your delivery location")
        self.assertContains(response, "Search Address in Addis Ababa")
        self.assertContains(response, "Choose an Eligible Agent First")

    def test_checkout_is_blocked_when_company_is_suspended(self):
        self.company.is_active = False
        self.company.save(update_fields=["is_active", "updated_at"])
        add_product_to_cart(self.user, self.product, 1)

        response = self.client.post(
            reverse("orders:checkout"),
            {
                "location_source": "current",
                "selected_agent_id": str(self.agent.pk),
                "delivery_address": "Piassa",
                "latitude": "9.031000",
                "longitude": "38.741000",
                "phone_number": "+251911000020",
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This company is not currently accepting new customer orders.")
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_prefills_default_saved_address(self):
        default_address = CustomerAddress.objects.create(
            user=self.user,
            label="Hotel",
            address_line="Meskel Square, Addis Ababa",
            latitude="9.012345",
            longitude="38.765432",
            notes="Use the service entrance",
            is_default=True,
        )
        add_product_to_cart(self.user, self.product, 1)

        response = self.client.get(reverse("orders:checkout"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["saved_address_id"], str(default_address.pk))
        self.assertEqual(response.context["form"].initial["delivery_address"], default_address.address_line)
        self.assertEqual(response.context["form"].initial["notes"], default_address.notes)

    def test_checkout_can_create_order_without_manually_typed_address_when_coordinates_exist(self):
        add_product_to_cart(self.user, self.product, 1)
        response = self.client.post(
            reverse("orders:checkout"),
            {
                "location_source": "map",
                "selected_agent_id": str(self.agent.pk),
                "delivery_address": "",
                "latitude": "9.031000",
                "longitude": "38.741000",
                "phone_number": "+251911000020",
                "notes": "",
            },
        )

        order = Order.objects.get()
        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        self.assertIn("Pinned location", order.delivery_address)
        self.assertEqual(order.status, OrderStatus.REQUESTED)
        self.assertEqual(order.agent_requests.count(), 1)

    def test_payment_page_refresh_redirects_to_chapa_checkout(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Kazanchis, Addis Ababa")
        self.accept_checkout_order(order)
        payment = order.payment
        payment.checkout_url = "https://checkout.chapa.co/pay/refreshed-session"

        with patch("orders.views.initialize_chapa_payment", return_value=payment) as initialize_mock:
            response = self.client.post(reverse("orders:payment", kwargs={"order_number": order.order_number}))

        self.assertRedirects(response, payment.checkout_url, fetch_redirect_response=False)
        initialize_mock.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAYMENT_PENDING)
        self.assertFalse(DeliveryConfirmation.objects.filter(order=order).exists())

    def test_payment_return_verifies_using_saved_reference_when_tx_ref_is_missing(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Kazanchis, Addis Ababa")
        self.accept_checkout_order(order)
        payment = order.payment

        with patch("orders.views.verify_chapa_payment") as verify_mock:
            response = self.client.get(reverse("orders:payment_success", kwargs={"order_number": order.order_number}))

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        verify_mock.assert_called_once_with(payment.reference)

    def test_full_delivery_flow_assigns_driver_and_updates_fefo_inventory(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-123", payload={"status": "success"})
        order.refresh_from_db()

        assign_driver(order, self.driver)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DRIVER_ASSIGNED)

        accept_delivery_assignment(order, self.driver_user)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DRIVER_ACCEPTED)

        mark_order_picked_up(order, self.driver_user)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PICKED_UP)

        start_delivery(order, self.driver_user)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.OUT_FOR_DELIVERY)

        mark_order_arrived(order, self.driver_user)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ARRIVED)

        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, "", confirmation.qr_payload_json, self.driver_user)
        order.refresh_from_db()
        self.inventory_batch.refresh_from_db()
        self.agent_stock.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assertEqual(self.inventory_batch.quantity_remaining, 4)
        self.assertEqual(self.agent_stock.available_quantity, 4)
        self.assertTrue(DeliveryConfirmation.objects.filter(order=order).exists())
        sale_transaction = InventoryTransaction.objects.get(reference=order.order_number)
        self.assertEqual(sale_transaction.transaction_type, InventoryTransactionType.SALE)
        self.assertEqual(sale_transaction.quantity_change, -2)

    def test_complete_delivery_accepts_full_qr_payload(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-456", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)

        confirmation = order.confirmation
        qr_payload = confirmation.qr_payload_json
        complete_delivery_and_deduct_stock(order, "", qr_payload, self.driver_user)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_driver_qr_confirm_endpoint_marks_delivery_complete(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-SCAN", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)

        self.client.force_login(self.driver_user)
        response = self.client.post(
            reverse("accounts:confirm_delivery_qr", kwargs={"order_number": order.order_number}),
            data=json.dumps({"qr_payload": order.confirmation.qr_payload_json}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "confirmed"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_complete_delivery_accepts_otp_only(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-OTP", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)

        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, "", self.driver_user)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_complete_delivery_recovers_when_batches_are_missing(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-BATCH", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)

        InventoryBatch.objects.filter(agent=self.agent, product=self.product).delete()
        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, "", self.driver_user)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assertTrue(
            InventoryBatch.objects.filter(agent=self.agent, product=self.product, batch_number=f"AUTO-SYNC-{self.product.id}").exists()
        )

    def test_order_history_and_tracking_views(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Kazanchis")
        list_response = self.client.get(reverse("orders:list"), {"search": order.order_number})
        tracking_response = self.client.get(reverse("orders:tracking", kwargs={"order_number": order.order_number}))

        self.assertContains(list_response, order.order_number)
        self.assertContains(tracking_response, "Current status")
        self.assertContains(tracking_response, "Live Delivery Map")

    def test_customer_can_reorder_a_delivered_order(self):
        delivered_order = self.create_delivered_order(quantity=2)

        response = self.client.post(reverse("orders:reorder", kwargs={"order_number": delivered_order.order_number}))

        self.assertRedirects(response, reverse("cart:detail"))
        cart = self.user.cart
        self.assertEqual(cart.items.count(), 1)
        cart_item = cart.items.get()
        self.assertEqual(cart_item.product, self.product)
        self.assertEqual(cart_item.quantity, 2)

    def test_customer_can_retry_checkout_after_agent_rejection(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        reject_agent_request(order.agent_requests.get(), note="Branch could not fulfill the request.")
        order.refresh_from_db()

        response = self.client.post(reverse("orders:retry_checkout", kwargs={"order_number": order.order_number}))

        self.assertRedirects(response, reverse("orders:checkout"))
        cart = self.user.cart
        self.assertEqual(cart.items.count(), 1)
        cart_item = cart.items.get()
        self.assertEqual(cart_item.product, self.product)
        self.assertEqual(cart_item.quantity, 2)

    def test_only_one_accept_attempt_can_succeed_for_same_pending_order(self):
        second_agent_manager = User.objects.create_user(
            email="agent-manager-two@example.com",
            password="StrongPass123!",
            first_name="Second",
            last_name="Manager",
            phone_number="+251911000024",
            role=UserRole.AGENT_MANAGER,
        )
        second_agent = Agent.objects.create(
            company=self.company,
            name="Fresh River Bole Agent",
            location_name="Bole",
            latitude="9.020000",
            longitude="38.760000",
            service_radius_km="20.00",
            is_active=True,
            is_accepting_orders=True,
            admin=second_agent_manager,
        )
        AgentStock.objects.create(
            agent=second_agent,
            product=self.product,
            available_quantity=6,
            reorder_level=2,
        )

        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        first_request = order.agent_requests.get()
        second_request = OrderAgentRequest.objects.create(
            order=order,
            agent=second_agent,
            status=AgentRequestStatus.PENDING,
            distance_km="2.50",
            note="Second agent racing the same pending order.",
        )

        accept_agent_request(first_request, note="Accepted first", accepted_by=self.agent_manager)

        # TestCase wraps each test in a transaction and the default SQLite test database does not
        # provide a realistic separate-connection concurrency harness for row locks, so this
        # regression simulates the race sequentially: the second accept must lose once the first
        # transition commits its status change under the locked order row.
        with self.assertRaisesMessage(ValidationError, "This order is no longer waiting for agent review."):
            accept_agent_request(second_request, note="Accepted second", accepted_by=second_agent_manager)

        order.refresh_from_db()
        first_request.refresh_from_db()
        second_request.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.PAYMENT_PENDING)
        self.assertEqual(order.selected_agent, self.agent)
        self.assertEqual(first_request.status, AgentRequestStatus.ACCEPTED)
        self.assertEqual(second_request.status, AgentRequestStatus.REJECTED)
        self.assertTrue(hasattr(order, "payment"))

    def test_customer_can_submit_driver_feedback_after_delivery(self):
        delivered_order = self.create_delivered_order()

        response = self.client.post(
            reverse("orders:submit_feedback", kwargs={"order_number": delivered_order.order_number}),
            {"rating": 5, "comment": "Fast delivery and polite driver."},
        )

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": delivered_order.order_number}))
        feedback = DeliveryFeedback.objects.get(order=delivered_order)
        self.assertEqual(feedback.rating, 5)
        self.assertEqual(feedback.comment, "Fast delivery and polite driver.")
        self.assertFalse(feedback.was_skipped)

    def test_customer_can_skip_driver_feedback_after_delivery(self):
        delivered_order = self.create_delivered_order()

        response = self.client.post(reverse("orders:skip_feedback", kwargs={"order_number": delivered_order.order_number}))

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": delivered_order.order_number}))
        feedback = DeliveryFeedback.objects.get(order=delivered_order)
        self.assertIsNone(feedback.rating)
        self.assertTrue(feedback.was_skipped)

    def test_tracking_status_json_returns_driver_coordinates(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Kazanchis")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-123", payload={"status": "success"})
        assign_driver(order, self.driver)
        DriverLocation.objects.create(
            driver_user=self.driver_user,
            latitude="9.050000",
            longitude="38.760000",
            is_online=True,
        )

        response = self.client.get(reverse("orders:tracking_status_json", kwargs={"order_number": order.order_number}))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["statusCode"], OrderStatus.DRIVER_ASSIGNED)
        self.assertEqual(payload["driver"]["name"], self.driver_user.full_name)
        self.assertEqual(payload["driver"]["latitude"], 9.05)
        self.assertEqual(payload["driver"]["longitude"], 38.76)
        self.assertTrue(payload["driver"]["online"])

    def test_customer_can_refresh_expired_qr_code(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-REFRESH", payload={"status": "success"})
        confirmation = order.confirmation
        old_token = confirmation.qr_token
        confirmation.expires_at = timezone.now() - timezone.timedelta(minutes=5)
        confirmation.save(update_fields=["expires_at", "updated_at"])

        response = self.client.post(reverse("orders:refresh_qr", kwargs={"order_number": order.order_number}))

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        confirmation.refresh_from_db()
        self.assertNotEqual(confirmation.qr_token, old_token)
        self.assertGreater(confirmation.expires_at, timezone.now())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_payment_confirmation_email_contains_price_location_and_code(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Bole Road, Addis Ababa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-EMAIL", payload={"status": "success"})

        self.assertGreaterEqual(len(mail.outbox), 1)
        receipt_message = next(message for message in mail.outbox if order.order_number in message.subject)
        self.assertIn(str(order.total), receipt_message.body)
        self.assertIn(order.delivery_address, receipt_message.body)
        self.assertIn(order.confirmation.otp_code, receipt_message.body)
        self.assertIn("Total paid", receipt_message.body)

    def test_agent_pending_orders_json_lists_pending_customer_request(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")

        self.client.force_login(self.agent_manager)
        response = self.client.get(reverse("orders:pending_orders_json"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["orders"]), 1)
        self.assertEqual(payload["orders"][0]["order_number"], order.order_number)

    def test_nearby_agents_preview_returns_matching_agent(self):
        add_product_to_cart(self.user, self.product, 1)
        response = self.client.get(
            reverse("orders:nearby_agents_preview"),
            {"latitude": "9.031000", "longitude": "38.741000"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["agents"]), 1)
        self.assertEqual(payload["agents"][0]["name"], self.agent.name)
        self.assertTrue(payload["agents"][0]["is_eligible"])

    def test_nearby_agents_preview_shows_nearby_agent_even_without_stock(self):
        self.agent_stock.available_quantity = 0
        self.agent_stock.save(update_fields=["available_quantity", "updated_at"])
        add_product_to_cart(self.user, self.product, 1)

        response = self.client.get(
            reverse("orders:nearby_agents_preview"),
            {"latitude": "9.031000", "longitude": "38.741000"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["agents"]), 1)
        self.assertFalse(payload["agents"][0]["is_eligible"])
        self.assertTrue(payload["agents"][0]["within_radius"])
        self.assertIn("enough stock", payload["message"])

    def test_agent_dashboard_displays_paid_order(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-789", payload={"status": "success"})

        self.client.force_login(self.agent_manager)
        response = self.client.get(reverse("accounts:agent_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)

    def test_customer_can_cancel_paid_order_with_partial_refund_inside_window(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-CANCEL", payload={"status": "success"})

        with patch(
            "orders.services.chapa_request",
            return_value={"status": "success", "data": {"status": "success", "reference": "RF-1"}},
        ):
            response = self.client.post(
                reverse("orders:cancel", kwargs={"order_number": order.order_number}),
                {"reason": "Changed my mind"},
            )

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        order.refresh_from_db()
        self.agent_stock.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment.status, PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(self.agent_stock.available_quantity, 6)
        cancellation_request = order.refund_requests.get()
        self.assertEqual(cancellation_request.status, RefundRequestStatus.PROCESSED)
        self.assertGreater(cancellation_request.approved_amount, 0)
        self.assertEqual(
            list(cancellation_request.status_history.values_list("title", flat=True)),
            ["Refund Requested", "Approved", "Refund Processed", "Completed"],
        )
        self.assertTrue(
            SupportActionLog.objects.filter(order=order, refund_request=cancellation_request, action="order_cancelled").exists()
        )

    def test_customer_can_request_refund_and_company_admin_can_approve_it(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-REFUND", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)
        mark_order_arrived(order, self.driver_user)
        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, "", self.driver_user)

        response = self.client.post(
            reverse("orders:request_refund", kwargs={"order_number": order.order_number}),
            {
                "category": ComplaintCategory.DAMAGED_PRODUCTS,
                "description": "The delivery arrived damaged and spilled.",
            },
        )
        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))

        complaint = order.complaints.get()
        self.assertEqual(complaint.category, ComplaintCategory.DAMAGED_PRODUCTS)
        self.assertEqual(complaint.status, ComplaintStatus.AWAITING_AGENT_RESPONSE)
        self.assertFalse(order.refund_requests.exists())
        self.assertIsNone(complaint.linked_refund_request)
        self.assertEqual(
            list(complaint.status_history.values_list("title", flat=True)),
            ["Complaint Submitted", "Awaiting Agent Response"],
        )

        self.client.force_login(self.company_admin)
        with patch(
            "orders.services.chapa_request",
            return_value={"status": "success", "data": {"status": "success", "reference": "RF-COMPLAINT-1"}},
        ):
            response = self.client.post(
                reverse("accounts:approve_refund", kwargs={"pk": complaint.pk}),
                {
                    "decision_reason": "Approved after delivery quality review.",
                },
            )
        self.assertRedirects(response, reverse("accounts:company_dashboard"))
        complaint.refresh_from_db()
        refund_request = complaint.linked_refund_request
        self.assertIsNotNone(refund_request)
        refund_request.refresh_from_db()
        order.payment.refresh_from_db()
        self.assertEqual(refund_request.status, RefundRequestStatus.PROCESSED)
        self.assertEqual(refund_request.payout_method, RefundPayoutMethod.GATEWAY)
        self.assertEqual(complaint.status, ComplaintStatus.DECISION_ISSUED)
        self.assertEqual(complaint.resolution_type, ComplaintResolutionType.FULL_REFUND)
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(
            list(refund_request.status_history.values_list("title", flat=True)),
            ["Approved", "Refund Processed", "Completed"],
        )
        self.assertEqual(
            list(complaint.status_history.values_list("title", flat=True)),
            ["Complaint Submitted", "Awaiting Agent Response", "Refund Processed", "Company Decision Issued"],
        )
        self.assertEqual(
            list(
                SupportActionLog.objects.filter(order=order, refund_request=refund_request, complaint=complaint)
                .values_list("action", flat=True)
            ),
            ["refund_processed", "complaint_company_decision_issued"],
        )

    def test_out_for_delivery_orders_cannot_be_cancelled(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.accept_checkout_order(order)
        mark_order_paid(order, reference="TX-OUT-DELIVERY", payload={"status": "success"})
        assign_driver(order, self.driver)
        accept_delivery_assignment(order, self.driver_user)
        mark_order_picked_up(order, self.driver_user)
        start_delivery(order, self.driver_user)

        with self.assertRaisesMessage(
            ValidationError,
            "Orders cannot be cancelled after they are marked out for delivery.",
        ):
            cancel_order(order, requested_by=self.user, reason="Too late")

    def test_delivered_orders_must_use_complaint_module_instead_of_cancellation(self):
        order = self.create_delivered_order()

        with self.assertRaisesMessage(
            ValidationError,
            "Delivered orders cannot be cancelled. Please use the complaint module instead.",
        ):
            cancel_order(order, requested_by=self.user, reason="Delivered already")

    def test_unauthorized_user_cannot_approve_or_process_refunds(self):
        order = self.create_delivered_order()
        refund_request = request_order_refund(
            order,
            requested_by=self.user,
            description="Need a review for a damaged delivery.",
            payout_method=RefundPayoutMethod.WALLET_CREDIT,
            category=ComplaintCategory.DAMAGED_PRODUCTS,
        )

        with self.assertRaisesMessage(ValidationError, "You are not authorized to approve this refund for this order."):
            approve_refund_request(
                refund_request,
                reviewed_by=self.user,
                approved_amount=order.total,
                resolution_note="Customer tried to self-approve.",
            )

        approve_refund_request(
            refund_request,
            reviewed_by=self.company_admin,
            approved_amount=order.total,
            resolution_note="Approved by the company admin.",
        )

        with self.assertRaisesMessage(ValidationError, "You are not authorized to process this refund for this order."):
            process_refund_request(refund_request, processed_by=self.user)

    def test_approved_refund_cannot_exceed_remaining_payment_amount(self):
        order = self.create_delivered_order()
        refund_request = request_order_refund(
            order,
            requested_by=self.user,
            description="Requesting more than the original payment should fail.",
            payout_method=RefundPayoutMethod.WALLET_CREDIT,
            category=ComplaintCategory.OTHER,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Approved refund amount cannot exceed the remaining refundable amount.",
        ):
            approve_refund_request(
                refund_request,
                reviewed_by=self.company_admin,
                approved_amount=order.total + Decimal("1.00"),
                resolution_note="Invalid amount.",
            )

    def test_checkout_applies_company_premium_discount_after_customer_streak(self):
        self.company.premium_feature_enabled = True
        self.company.premium_streak_threshold = 2
        self.company.premium_discount_percent = "10.00"
        self.company.save()

        for index in range(2):
            prior_order = Order.objects.create(
                customer=self.user,
                company=self.company,
                selected_agent=self.agent,
                order_number=f"ORD-STREAK{index}",
                status=OrderStatus.DELIVERED,
                delivery_address="Piassa",
                latitude="9.031000",
                longitude="38.741000",
                phone_number="+251911000020",
                subtotal="18.00",
                delivery_fee="5.00",
                total="23.00",
                delivered_at=timezone.now(),
                paid_at=timezone.now(),
            )
            prior_order.items.create(
                product=self.product,
                product_name=self.product.name,
                unit_price="18.00",
                quantity=1,
            )

        response, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("orders:detail", kwargs={"order_number": order.order_number}))
        self.assertEqual(order.discount_amount, order.subtotal * order.premium_discount_percent / 100)
        self.assertEqual(order.total, order.subtotal - order.discount_amount + order.delivery_fee)
        self.assertEqual(order.status, OrderStatus.REQUESTED)

    def test_premium_discount_resets_streak_after_discounted_purchase(self):
        self.company.premium_feature_enabled = True
        self.company.premium_streak_threshold = 2
        self.company.premium_discount_percent = "10.00"
        self.company.save()

        self.create_delivered_order()
        self.create_delivered_order()

        _, reward_order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.assertEqual(reward_order.premium_streak_count, 2)
        self.assertGreater(reward_order.discount_amount, 0)

        self.deliver_existing_order(reward_order)

        _, reset_order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.assertEqual(reset_order.discount_amount, Decimal("0.00"))
        self.assertEqual(reset_order.premium_discount_percent, Decimal("0.00"))
        self.assertEqual(reset_order.premium_streak_count, 0)

        self.deliver_existing_order(reset_order)

        _, rebuilding_order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        self.assertEqual(rebuilding_order.discount_amount, Decimal("0.00"))
        self.assertEqual(rebuilding_order.premium_streak_count, 1)
