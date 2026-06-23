from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import DecimalField, ExpressionWrapper, F, Sum

from core.models import TimeStampedModel


class Cart(TimeStampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cart")

    class Meta:
        ordering = ("-updated_at",)

    def __str__(self):
        return f"Cart for {self.user.email}"

    @property
    def company(self):
        first_item = self.items.select_related("product__company").first()
        return first_item.product.company if first_item else None

    @property
    def items_count(self):
        return self.items.aggregate(total=Sum("quantity")).get("total") or 0

    @property
    def subtotal(self):
        line_total = ExpressionWrapper(
            F("quantity") * F("unit_price"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        return self.items.aggregate(total=Sum(line_total)).get("total") or Decimal("0.00")

    @property
    def delivery_fee(self):
        return Decimal(str(settings.DEFAULT_DELIVERY_FEE)) if self.items.exists() else Decimal("0.00")

    @property
    def grand_total(self):
        return self.subtotal + self.delivery_fee


class CartItem(TimeStampedModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="cart_items")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(fields=["cart", "product"], name="unique_cart_product"),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    def clean(self):
        if not self.product.is_active:
            raise ValidationError("This product is no longer available.")
        if self.quantity > self.product.available_quantity:
            raise ValidationError({"quantity": "Requested quantity exceeds available stock."})

    def save(self, *args, **kwargs):
        if not self.unit_price:
            self.unit_price = self.product.price
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def line_total(self):
        return self.unit_price * self.quantity

# Create your models here.
