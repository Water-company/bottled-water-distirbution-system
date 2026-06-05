from unittest.mock import patch

from django.utils import timezone
from django.test import TestCase
from django.urls import reverse

from accounts.models import User, UserRole
from cart.services import add_product_to_cart
from catalog.models import Agent, AgentStock, Company, Driver, InventoryBatch, Product
from core.models import DriverLocation
from orders.models import AgentRequestStatus, DeliveryConfirmation, Order, OrderStatus, PaymentStatus, RefundRequestStatus
from orders.services import approve_refund_request, assign_driver, complete_delivery_and_deduct_stock, mark_order_paid, start_delivery


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
        self.company_admin = User.objects.create_user(
            email="company-admin@example.com",
            password="StrongPass123!",
            first_name="Company",
            last_name="Admin",
            phone_number="+251911000023",
            role=UserRole.COMPANY_ADMIN,
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
            admin=self.company_admin,
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
        with patch("orders.views.initialize_chapa_payment", return_value=self.checkout_payment_stub()):
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

    def test_checkout_redirects_directly_to_chapa_and_reserves_stock(self):
        response, order = self.create_checkout_order(quantity=2, delivery_address="Bole Road, Addis Ababa", notes="Leave at reception")

        self.assertRedirects(response, "https://checkout.chapa.co/pay/test-session", fetch_redirect_response=False)
        self.assertEqual(order.company, self.company)
        self.assertEqual(order.status, OrderStatus.PAYMENT_PENDING)
        self.assertEqual(order.selected_agent, self.agent)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.agent_requests.count(), 1)
        self.assertEqual(order.agent_requests.get().status, AgentRequestStatus.ACCEPTED)
        self.assertTrue(hasattr(order, "payment"))
        self.assertEqual(order.payment.status, PaymentStatus.PENDING)
        self.agent_stock.refresh_from_db()
        self.assertEqual(self.agent_stock.available_quantity, 4)

    def test_checkout_page_loads_successfully(self):
        add_product_to_cart(self.user, self.product, 1)
        response = self.client.get(reverse("orders:checkout"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose your delivery location")
        self.assertContains(response, "Search Address in Addis Ababa")
        self.assertContains(response, "Choose an Eligible Agent First")

    def test_checkout_can_create_order_without_manually_typed_address_when_coordinates_exist(self):
        add_product_to_cart(self.user, self.product, 1)
        with patch("orders.views.initialize_chapa_payment", return_value=self.checkout_payment_stub()):
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
        self.assertRedirects(response, "https://checkout.chapa.co/pay/test-session", fetch_redirect_response=False)
        self.assertIn("Pinned location", order.delivery_address)
        self.assertEqual(order.status, OrderStatus.PAYMENT_PENDING)
        self.assertEqual(order.agent_requests.count(), 1)

    def test_payment_page_refresh_redirects_to_chapa_checkout(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Kazanchis, Addis Ababa")
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
        payment = order.payment

        with patch("orders.views.verify_chapa_payment") as verify_mock:
            response = self.client.get(reverse("orders:payment_success", kwargs={"order_number": order.order_number}))

        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))
        verify_mock.assert_called_once_with(payment.reference)

    def test_full_delivery_flow_assigns_driver_and_updates_fefo_inventory(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")

        mark_order_paid(order, reference="TX-123", payload={"status": "success"})
        order.refresh_from_db()

        assign_driver(order, self.driver)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DRIVER_ASSIGNED)

        start_delivery(order, self.driver_user)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.OUT_FOR_DELIVERY)

        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, confirmation.qr_token, self.driver_user)
        order.refresh_from_db()
        self.inventory_batch.refresh_from_db()
        self.agent_stock.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assertEqual(self.inventory_batch.quantity_remaining, 4)
        self.assertEqual(self.agent_stock.available_quantity, 4)
        self.assertTrue(DeliveryConfirmation.objects.filter(order=order).exists())

    def test_complete_delivery_accepts_full_qr_payload(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        mark_order_paid(order, reference="TX-456", payload={"status": "success"})
        assign_driver(order, self.driver)
        start_delivery(order, self.driver_user)

        confirmation = order.confirmation
        qr_payload = f"{order.order_number}|{confirmation.qr_token}|{confirmation.otp_code}"
        complete_delivery_and_deduct_stock(order, "", qr_payload, self.driver_user)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_complete_delivery_accepts_otp_only(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        mark_order_paid(order, reference="TX-OTP", payload={"status": "success"})
        assign_driver(order, self.driver)
        start_delivery(order, self.driver_user)

        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, "", self.driver_user)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_complete_delivery_recovers_when_batches_are_missing(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        mark_order_paid(order, reference="TX-BATCH", payload={"status": "success"})
        assign_driver(order, self.driver)
        start_delivery(order, self.driver_user)

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

    def test_tracking_status_json_returns_driver_coordinates(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Kazanchis")
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

    def test_agent_pending_orders_json_is_empty_for_auto_reserved_checkout(self):
        self.create_checkout_order(quantity=1, delivery_address="Piassa")

        self.client.force_login(self.agent_manager)
        response = self.client.get(reverse("orders:pending_orders_json"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"orders": []})

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
        mark_order_paid(order, reference="TX-789", payload={"status": "success"})

        self.client.force_login(self.agent_manager)
        response = self.client.get(reverse("accounts:agent_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)

    def test_customer_can_cancel_paid_order_with_partial_refund_inside_window(self):
        _, order = self.create_checkout_order(quantity=2, delivery_address="Piassa")
        mark_order_paid(order, reference="TX-CANCEL", payload={"status": "success"})

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
        self.assertEqual(cancellation_request.status, RefundRequestStatus.APPROVED)
        self.assertGreater(cancellation_request.approved_amount, 0)

    def test_customer_can_request_refund_and_company_admin_can_approve_it(self):
        _, order = self.create_checkout_order(quantity=1, delivery_address="Piassa")
        mark_order_paid(order, reference="TX-REFUND", payload={"status": "success"})
        assign_driver(order, self.driver)
        start_delivery(order, self.driver_user)
        confirmation = order.confirmation
        complete_delivery_and_deduct_stock(order, confirmation.otp_code, "", self.driver_user)

        response = self.client.post(
            reverse("orders:request_refund", kwargs={"order_number": order.order_number}),
            {"reason": "The delivery arrived damaged and spilled."},
        )
        self.assertRedirects(response, reverse("orders:detail", kwargs={"order_number": order.order_number}))

        refund_request = order.refund_requests.get(request_type="service_issue")
        self.assertEqual(refund_request.status, RefundRequestStatus.PENDING)

        self.client.force_login(self.company_admin)
        response = self.client.post(
            reverse("accounts:approve_refund", kwargs={"pk": refund_request.pk}),
            {
                "approved_amount": str(order.total),
                "resolution_note": "Approved after delivery quality review.",
            },
        )
        self.assertRedirects(response, reverse("accounts:company_dashboard"))
        refund_request.refresh_from_db()
        order.payment.refresh_from_db()
        self.assertEqual(refund_request.status, RefundRequestStatus.APPROVED)
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)

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

        self.assertRedirects(response, "https://checkout.chapa.co/pay/test-session", fetch_redirect_response=False)
        self.assertEqual(order.discount_amount, order.subtotal * order.premium_discount_percent / 100)
        self.assertEqual(order.total, order.subtotal - order.discount_amount + order.delivery_fee)
