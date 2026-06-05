from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from cart.models import CartItem
from cart.services import add_product_to_cart
from catalog.models import Company, Product


class CartFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cart@example.com",
            password="StrongPass123!",
            first_name="Noah",
            last_name="Lake",
            phone_number="+251911000010",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.company = Company.objects.create(
            name="Pure Water",
            description="Trusted supplier",
            location="Addis Ababa",
            is_verified=True,
        )
        self.product = Product.objects.create(
            company=self.company,
            name="Starter Pack",
            description="Starter product",
            price="12.00",
            available_quantity=5,
        )

    def test_add_update_and_remove_cart_item(self):
        response = self.client.post(reverse("cart:add", kwargs={"slug": self.product.slug}), {"quantity": 2})
        self.assertRedirects(response, reverse("cart:detail"))
        item = CartItem.objects.get()
        self.assertEqual(item.quantity, 2)

        response = self.client.post(reverse("cart:update", kwargs={"pk": item.pk}), {"quantity": 3})
        self.assertRedirects(response, reverse("cart:detail"))
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

        response = self.client.post(reverse("cart:remove", kwargs={"pk": item.pk}))
        self.assertRedirects(response, reverse("cart:detail"))
        self.assertFalse(CartItem.objects.exists())

    def test_cannot_add_quantity_above_stock(self):
        self.client.post(reverse("cart:add", kwargs={"slug": self.product.slug}), {"quantity": 99})
        self.assertFalse(CartItem.objects.exists())

    def test_cart_rejects_products_from_multiple_companies(self):
        other_company = Company.objects.create(
            name="Other Water",
            description="Another supplier",
            location="Adama",
            is_verified=True,
        )
        other_product = Product.objects.create(
            company=other_company,
            name="Other Pack",
            description="Different company product",
            price="8.00",
            available_quantity=5,
        )

        add_product_to_cart(self.user, self.product, 1)
        with self.assertRaisesMessage(Exception, "one company"):
            add_product_to_cart(self.user, other_product, 1)

# Create your tests here.
