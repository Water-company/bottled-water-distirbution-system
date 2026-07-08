from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserRole
from catalog.models import Agent, Company, Product
from core.models import Notification


User = get_user_model()


class HomeLandingViewTests(TestCase):
    def test_home_shows_guest_auth_actions(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:register"))
        self.assertEqual(response.context["portal_url"], reverse("home"))
        self.assertEqual(response.context["unread_notifications_count"], 0)

    def test_home_shows_customer_dashboard_and_unread_notifications(self):
        user = User.objects.create_user(
            email="customer-home@example.com",
            password="StrongPass123!",
            first_name="Liya",
            last_name="Bekele",
            phone_number="+251911110001",
        )
        Notification.objects.create(
            recipient=user,
            title="Order update",
            message="Your delivery is on the way.",
        )
        Notification.objects.create(
            recipient=user,
            title="Read notice",
            message="This notification is already read.",
            is_read=True,
        )

        self.client.force_login(user)
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("accounts:dashboard"))
        self.assertContains(response, reverse("accounts:notifications"))
        self.assertContains(response, reverse("accounts:profile"))
        self.assertEqual(response.context["portal_url"], reverse("accounts:dashboard"))
        self.assertEqual(response.context["unread_notifications_count"], 1)

    def test_home_uses_role_specific_dashboard_link_for_system_admins(self):
        system_admin = User.objects.create_superuser(
            email="system-home@example.com",
            password="StrongPass123!",
            first_name="Admin",
            last_name="User",
            phone_number="+251911110002",
        )

        self.client.force_login(system_admin)
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("accounts:system_dashboard"))
        self.assertEqual(response.context["portal_url"], reverse("accounts:system_dashboard"))

    def test_home_company_cards_send_internal_roles_to_their_dashboard(self):
        system_admin = User.objects.create_user(
            email="system-home-cards@example.com",
            password="StrongPass123!",
            first_name="Admin",
            last_name="Cards",
            phone_number="+251911110003",
            role=UserRole.SYSTEM_ADMIN,
        )
        company = Company.objects.create(
            name="Card Ready Water",
            description="Verified company for landing cards",
            location="Addis Ababa",
            is_verified=True,
        )
        Product.objects.create(
            company=company,
            name="Card Product",
            description="Ready for customer browsing",
            price="25.00",
            available_quantity=12,
        )
        Agent.objects.create(
            company=company,
            name="Card Agent",
            location_name="Bole",
            latitude="9.010000",
            longitude="38.760000",
            service_radius_km="12.00",
            is_active=True,
            is_accepting_orders=True,
        )

        self.client.force_login(system_admin)
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f"{reverse('products:list')}?company={company.pk}")
