from django.core.exceptions import ValidationError

from cart.models import Cart, CartItem


def get_or_create_cart(user):
    cart, _ = Cart.objects.get_or_create(user=user)
    return cart


def add_product_to_cart(user, product, quantity):
    cart = get_or_create_cart(user)
    existing_company = cart.company
    if existing_company and existing_company.pk != product.company_id:
        raise ValidationError(
            "Your cart can only contain products from one company at a time. Clear the cart to switch companies."
        )
    item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        defaults={"quantity": quantity, "unit_price": product.price},
    )
    if not created:
        item.quantity += quantity
        item.unit_price = product.price
    item.full_clean()
    item.save()
    return item


def update_cart_item(item, quantity):
    item.quantity = quantity
    item.full_clean()
    item.save()
    return item


def remove_cart_item(item):
    item.delete()
