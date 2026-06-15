import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.services import get_company_admin_users
from catalog.models import (
    AgentBatchSale,
    AgentBatchSalePayment,
    AgentBatchSalePaymentStatus,
    AgentBatchSalePaymentType,
    AgentBatchSaleStatus,
    AgentStock,
    CompanyBatch,
    CompanyBatchStatus,
    InventoryBatch,
    InventoryTransaction,
    InventoryTransactionType,
    Product,
)
from core.services import notify_user


def _safe_money(value):
    if value in (None, ""):
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def create_inventory_transaction(
    *,
    agent,
    product,
    transaction_type,
    quantity_change,
    stock_after,
    performed_by=None,
    batch=None,
    reference="",
    note="",
):
    return InventoryTransaction.objects.create(
        agent=agent,
        product=product,
        batch=batch,
        performed_by=performed_by,
        transaction_type=transaction_type,
        quantity_change=quantity_change,
        stock_after=stock_after,
        reference=reference,
        note=note,
    )


def _notify_company_admins(company, title, message, link):
    for admin_user in get_company_admin_users(company):
        notify_user(admin_user, title, message, link=link)


def _generate_company_batch_number(company, index):
    year = timezone.localdate().year
    base = f"BATCH-{year}-{company.pk:03d}-{index:03d}"
    batch_number = base
    suffix = 1
    while CompanyBatch.objects.filter(company=company, batch_number=batch_number).exists():
        batch_number = f"{base}-{suffix}"
        suffix += 1
    return batch_number


@transaction.atomic
def create_company_starter_catalog(*, company, created_by=None):
    starter_rows = (
        {
            "name": "0.5L Daily Sport",
            "size_label": "0.5L",
            "description": "Single-serve bottled water for events, gyms, and quick retail pickup.",
            "price": Decimal("35.00"),
            "available_quantity": 1200,
        },
        {
            "name": "5L Family Pack",
            "size_label": "5L",
            "description": "Mid-size bottled water for homes, cafes, and family delivery orders.",
            "price": Decimal("120.00"),
            "available_quantity": 450,
        },
        {
            "name": "20L Office Pro",
            "size_label": "20L",
            "description": "Large refill jars for offices, hotels, and commercial venues.",
            "price": Decimal("140.00"),
            "available_quantity": 300,
        },
    )
    created_products = []
    created_batches = []
    agents = list(company.agents.all())

    for index, row in enumerate(starter_rows, start=1):
        product, created = Product.objects.get_or_create(
            company=company,
            name=row["name"],
            defaults=row,
        )
        if not created:
            updated_fields = []
            for field_name in ("size_label", "description", "price"):
                if not getattr(product, field_name):
                    setattr(product, field_name, row[field_name])
                    updated_fields.append(field_name)
            if product.available_quantity <= 0:
                product.available_quantity = row["available_quantity"]
                updated_fields.append("available_quantity")
            if updated_fields:
                product.save(update_fields=updated_fields + ["updated_at"])
        else:
            created_products.append(product)

        for agent in agents:
            AgentStock.objects.get_or_create(
                agent=agent,
                product=product,
                defaults={"available_quantity": 0, "reorder_level": 0},
            )

        if not product.production_batches.exists():
            batch = CompanyBatch.objects.create(
                company=company,
                product=product,
                batch_number=_generate_company_batch_number(company, index),
                production_date=timezone.localdate(),
                total_cases_produced=product.available_quantity or row["available_quantity"],
                unit_price=product.price,
                created_by=created_by,
                note="Starter batch generated to make the company catalog immediately requestable by agent branches.",
            )
            created_batches.append(batch)

    return created_products, created_batches


def get_agent_open_batch_balance(agent, exclude_sale=None):
    sales = agent.batch_sales.filter(status=AgentBatchSaleStatus.APPROVED).prefetch_related("payments")
    if exclude_sale is not None and exclude_sale.pk:
        sales = sales.exclude(pk=exclude_sale.pk)
    return sum(sale.outstanding_balance for sale in sales)


def _ensure_batch_sync_for_stock(agent, product, stock):
    total_batch_quantity = (
        InventoryBatch.objects.filter(agent=agent, product=product, quantity_remaining__gt=0)
        .aggregate(total=models.Sum("quantity_remaining"))
        .get("total")
        or 0
    )
    missing_quantity = stock.available_quantity - total_batch_quantity
    if missing_quantity <= 0:
        return

    today = timezone.localdate()
    InventoryBatch.objects.create(
        agent=agent,
        product=product,
        batch_number=f"AUTO-ADJUST-{product.id}-{uuid.uuid4().hex[:6].upper()}",
        quantity_received=missing_quantity,
        quantity_remaining=missing_quantity,
        base_unit_cost=Decimal("0.00"),
        expires_at=today + timezone.timedelta(days=365),
        received_at=today,
    )


def _sync_company_batch_status(batch):
    if batch.status == CompanyBatchStatus.RECALLED:
        return
    next_status = CompanyBatchStatus.CLOSED if batch.unsold_cases_remaining <= 0 else CompanyBatchStatus.AVAILABLE
    if batch.status != next_status:
        batch.status = next_status
        batch.save(update_fields=["status", "updated_at"])


def _get_or_create_agent_inventory_batch(agent, company_batch, quantity, unit_price):
    today = timezone.localdate()
    inventory_batch, _ = InventoryBatch.objects.select_for_update().get_or_create(
        agent=agent,
        batch_number=company_batch.batch_number,
        defaults={
            "product": company_batch.product,
            "quantity_received": 0,
            "quantity_remaining": 0,
            "base_unit_cost": unit_price,
            "expires_at": today + timezone.timedelta(days=365),
            "received_at": today,
        },
    )
    if inventory_batch.product_id != company_batch.product_id:
        raise ValidationError("This batch number is already tied to a different product in agent inventory.")
    inventory_batch.product = company_batch.product
    inventory_batch.quantity_received += quantity
    inventory_batch.quantity_remaining += quantity
    inventory_batch.base_unit_cost = unit_price
    inventory_batch.received_at = today
    inventory_batch.save(
        update_fields=[
            "product",
            "quantity_received",
            "quantity_remaining",
            "base_unit_cost",
            "received_at",
            "updated_at",
        ]
    )
    return inventory_batch


@transaction.atomic
def submit_agent_batch_sale_request(
    *,
    agent,
    batch,
    requested_by,
    quantity_requested,
    payment_type,
    requested_upfront_amount=Decimal("0.00"),
    requested_note="",
):
    if batch.company_id != agent.company_id:
        raise ValidationError("You can only request stock from batches owned by your company.")
    if not batch.can_allocate:
        raise ValidationError("This batch is not available for new stock requests.")
    if quantity_requested < 1:
        raise ValidationError("Request at least one case.")
    if quantity_requested > batch.unsold_cases_remaining:
        raise ValidationError(f"Only {batch.unsold_cases_remaining} cases remain in this batch.")

    requested_upfront_amount = _safe_money(requested_upfront_amount)
    invoice_estimate = _safe_money(batch.unit_price) * quantity_requested
    current_outstanding = get_agent_open_batch_balance(agent)
    if agent.credit_limit and current_outstanding > agent.credit_limit:
        raise ValidationError(
            f"Your outstanding balance is {current_outstanding}, above the credit limit of {agent.credit_limit}. "
            "Settle existing stock sales before requesting more."
        )

    if payment_type == AgentBatchSalePaymentType.FULL and requested_upfront_amount <= 0:
        requested_upfront_amount = invoice_estimate
    estimated_outstanding = max(invoice_estimate - requested_upfront_amount, Decimal("0.00"))
    if agent.credit_limit and current_outstanding + estimated_outstanding > agent.credit_limit:
        raise ValidationError(
            f"This request would push your balance above the credit limit of {agent.credit_limit}. "
            f"Current open balance: {current_outstanding}."
        )

    sale = AgentBatchSale.objects.create(
        agent=agent,
        batch=batch,
        requested_by=requested_by,
        quantity_requested=quantity_requested,
        payment_type=payment_type,
        requested_upfront_amount=requested_upfront_amount,
        unit_price=batch.unit_price,
        requested_note=requested_note,
    )
    _notify_company_admins(
        batch.company,
        "New batch stock request",
        f"{agent.name} requested {quantity_requested} cases from batch {batch.batch_number}.",
        link=reverse("accounts:company_inventory"),
    )
    if agent.admin and agent.admin_id != requested_by.id:
        notify_user(
            agent.admin,
            "Stock request submitted",
            f"{quantity_requested} cases from {batch.batch_number} were submitted for approval.",
            link=reverse("accounts:agent_inventory"),
        )
    return sale


@transaction.atomic
def approve_agent_batch_sale(
    *,
    sale,
    approved_by,
    quantity_approved,
    unit_price,
    initial_payment_amount=Decimal("0.00"),
    credit_due_date=None,
    decision_note="",
):
    sale = AgentBatchSale.objects.select_related("agent", "batch", "batch__company", "requested_by").get(pk=sale.pk)
    batch = CompanyBatch.objects.select_for_update().get(pk=sale.batch_id)
    if sale.status != AgentBatchSaleStatus.PENDING:
        raise ValidationError("This stock request has already been reviewed.")
    if batch.status == CompanyBatchStatus.RECALLED:
        raise ValidationError("You cannot approve stock from a recalled batch.")
    if quantity_approved < 1:
        raise ValidationError("Approve at least one case.")
    if quantity_approved > batch.unsold_cases_remaining:
        raise ValidationError(f"Only {batch.unsold_cases_remaining} cases remain in batch {batch.batch_number}.")

    unit_price = _safe_money(unit_price)
    initial_payment_amount = _safe_money(initial_payment_amount)
    total_amount = unit_price * quantity_approved
    payment_type = sale.payment_type

    if payment_type == AgentBatchSalePaymentType.FULL:
        if initial_payment_amount != total_amount:
            raise ValidationError("Full payment requests must be settled in full at approval.")
        credit_due_date = None
    elif payment_type == AgentBatchSalePaymentType.PARTIAL:
        if initial_payment_amount <= 0 or initial_payment_amount >= total_amount:
            raise ValidationError("Partial payment must be more than zero and less than the full invoice.")
        if not credit_due_date:
            raise ValidationError("Partial payment approvals require a due date for the remaining balance.")
    else:
        if initial_payment_amount != 0:
            raise ValidationError("Credit approvals cannot record an upfront payment.")
        if not credit_due_date:
            raise ValidationError("Credit approvals require a due date.")

    projected_outstanding = get_agent_open_batch_balance(sale.agent, exclude_sale=sale) + (total_amount - initial_payment_amount)
    if sale.agent.credit_limit and projected_outstanding > sale.agent.credit_limit:
        raise ValidationError(
            f"Approving this request would exceed the agent credit limit of {sale.agent.credit_limit}."
        )

    batch.unsold_cases_remaining -= quantity_approved
    batch.save(update_fields=["unsold_cases_remaining", "updated_at"])
    _sync_company_batch_status(batch)

    sale.quantity_approved = quantity_approved
    sale.unit_price = unit_price
    sale.credit_due_date = credit_due_date
    sale.status = AgentBatchSaleStatus.APPROVED
    sale.decision_note = decision_note
    sale.approved_by = approved_by
    sale.approved_at = timezone.now()
    sale.rejected_at = None
    sale.save(
        update_fields=[
            "quantity_approved",
            "unit_price",
            "credit_due_date",
            "status",
            "decision_note",
            "approved_by",
            "approved_at",
            "rejected_at",
            "updated_at",
        ]
    )

    stock, _ = AgentStock.objects.select_for_update().get_or_create(
        agent=sale.agent,
        product=batch.product,
        defaults={"available_quantity": 0, "reorder_level": 0},
    )
    stock.available_quantity += quantity_approved
    stock.save(update_fields=["available_quantity", "updated_at"])
    inventory_batch = _get_or_create_agent_inventory_batch(sale.agent, batch, quantity_approved, unit_price)
    create_inventory_transaction(
        agent=sale.agent,
        product=batch.product,
        transaction_type=InventoryTransactionType.RESTOCK,
        quantity_change=quantity_approved,
        stock_after=stock.available_quantity,
        performed_by=approved_by,
        batch=inventory_batch,
        reference=f"BATCH-SALE-{sale.pk}",
        note=f"Received from company batch {batch.batch_number}.",
    )

    if initial_payment_amount > 0:
        AgentBatchSalePayment.objects.create(
            sale=sale,
            amount=initial_payment_amount,
            submitted_by=approved_by,
            confirmed_by=approved_by,
            status=AgentBatchSalePaymentStatus.CONFIRMED,
            submitted_note="Recorded at approval.",
            confirmed_at=timezone.now(),
        )

    if sale.agent.admin:
        notify_user(
            sale.agent.admin,
            "Stock request approved",
            f"{quantity_approved} cases from {batch.batch_number} were approved for {sale.agent.name}.",
            link=reverse("accounts:agent_inventory"),
        )
    if sale.requested_by and sale.requested_by_id != sale.agent.admin_id:
        notify_user(
            sale.requested_by,
            "Stock request approved",
            f"Your request for batch {batch.batch_number} was approved.",
            link=reverse("accounts:agent_inventory"),
        )
    return sale


@transaction.atomic
def reject_agent_batch_sale(*, sale, reviewed_by, decision_note):
    sale = AgentBatchSale.objects.select_related("agent", "batch", "requested_by").get(pk=sale.pk)
    if sale.status != AgentBatchSaleStatus.PENDING:
        raise ValidationError("This stock request has already been reviewed.")
    decision_note = (decision_note or "").strip()
    if not decision_note:
        raise ValidationError("A rejection reason is required.")

    sale.status = AgentBatchSaleStatus.REJECTED
    sale.decision_note = decision_note
    sale.approved_by = reviewed_by
    sale.rejected_at = timezone.now()
    sale.save(update_fields=["status", "decision_note", "approved_by", "rejected_at", "updated_at"])
    if sale.agent.admin:
        notify_user(
            sale.agent.admin,
            "Stock request rejected",
            f"{sale.batch.batch_number} was rejected for {sale.agent.name}: {decision_note}",
            link=reverse("accounts:agent_inventory"),
        )
    if sale.requested_by and sale.requested_by_id != sale.agent.admin_id:
        notify_user(
            sale.requested_by,
            "Stock request rejected",
            f"Your request for batch {sale.batch.batch_number} was rejected: {decision_note}",
            link=reverse("accounts:agent_inventory"),
        )
    return sale


@transaction.atomic
def submit_agent_batch_sale_payment(*, sale, submitted_by, amount, note=""):
    sale = AgentBatchSale.objects.select_related("agent", "batch", "batch__company").get(pk=sale.pk)
    if sale.status != AgentBatchSaleStatus.APPROVED:
        raise ValidationError("Payments can only be logged against approved stock sales.")
    amount = _safe_money(amount)
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")
    if amount > sale.outstanding_balance:
        raise ValidationError(f"The open balance for this sale is only {sale.outstanding_balance}.")

    payment = AgentBatchSalePayment.objects.create(
        sale=sale,
        amount=amount,
        submitted_by=submitted_by,
        submitted_note=note,
    )
    _notify_company_admins(
        sale.batch.company,
        "Agent payment submitted",
        f"{sale.agent.name} submitted a payment of {amount} for batch {sale.batch.batch_number}.",
        link=reverse("accounts:company_inventory"),
    )
    return payment


@transaction.atomic
def confirm_agent_batch_sale_payment(*, payment, confirmed_by):
    payment = AgentBatchSalePayment.objects.select_related("sale", "sale__agent", "sale__batch").get(pk=payment.pk)
    if payment.status != AgentBatchSalePaymentStatus.PENDING:
        raise ValidationError("This payment submission has already been reviewed.")
    if payment.amount > payment.sale.outstanding_balance:
        raise ValidationError(f"Only {payment.sale.outstanding_balance} remains outstanding on this sale.")

    payment.status = AgentBatchSalePaymentStatus.CONFIRMED
    payment.confirmed_by = confirmed_by
    payment.confirmed_at = timezone.now()
    payment.rejection_reason = ""
    payment.save(update_fields=["status", "confirmed_by", "confirmed_at", "rejection_reason", "updated_at"])
    if payment.sale.agent.admin:
        notify_user(
            payment.sale.agent.admin,
            "Agent payment confirmed",
            f"Your payment of {payment.amount} for batch {payment.sale.batch.batch_number} was confirmed.",
            link=reverse("accounts:agent_inventory"),
        )
    return payment


@transaction.atomic
def reject_agent_batch_sale_payment(*, payment, confirmed_by, rejection_reason):
    payment = AgentBatchSalePayment.objects.select_related("sale", "sale__agent", "sale__batch").get(pk=payment.pk)
    if payment.status != AgentBatchSalePaymentStatus.PENDING:
        raise ValidationError("This payment submission has already been reviewed.")
    rejection_reason = (rejection_reason or "").strip()
    if not rejection_reason:
        raise ValidationError("A rejection reason is required.")

    payment.status = AgentBatchSalePaymentStatus.REJECTED
    payment.confirmed_by = confirmed_by
    payment.confirmed_at = timezone.now()
    payment.rejection_reason = rejection_reason
    payment.save(update_fields=["status", "confirmed_by", "confirmed_at", "rejection_reason", "updated_at"])
    if payment.sale.agent.admin:
        notify_user(
            payment.sale.agent.admin,
            "Agent payment rejected",
            f"Your payment for batch {payment.sale.batch.batch_number} was rejected: {rejection_reason}",
            link=reverse("accounts:agent_inventory"),
        )
    return payment


@transaction.atomic
def recall_company_batch(*, batch, recalled_by, reason):
    batch = CompanyBatch.objects.select_related("company").get(pk=batch.pk)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Provide a recall reason before triggering a batch recall.")
    if batch.status == CompanyBatchStatus.RECALLED:
        raise ValidationError("This batch has already been recalled.")

    recoverable_at_agents = (
        InventoryBatch.objects.filter(
            agent__company=batch.company,
            product=batch.product,
            batch_number=batch.batch_number,
        ).aggregate(total=models.Sum("quantity_remaining")).get("total")
        or 0
    )
    batch.status = CompanyBatchStatus.RECALLED
    batch.recall_reason = reason
    batch.recalled_cases = batch.unsold_cases_remaining + recoverable_at_agents
    batch.recalled_at = timezone.now()
    batch.recalled_by = recalled_by
    batch.save(
        update_fields=[
            "status",
            "recall_reason",
            "recalled_cases",
            "recalled_at",
            "recalled_by",
            "updated_at",
        ]
    )

    affected_sales = batch.agent_sales.filter(status=AgentBatchSaleStatus.APPROVED).select_related("agent__admin")
    for sale in affected_sales:
        remaining_with_agent = (
            InventoryBatch.objects.filter(
                agent=sale.agent,
                product=batch.product,
                batch_number=batch.batch_number,
            ).aggregate(total=models.Sum("quantity_remaining")).get("total")
            or 0
        )
        if sale.agent.admin:
            notify_user(
                sale.agent.admin,
                "Batch recall issued",
                f"Batch {batch.batch_number} was recalled. Your branch received {sale.quantity_approved} cases and still holds {remaining_with_agent}.",
                link=reverse("accounts:agent_inventory"),
            )
    return batch


@transaction.atomic
def apply_agent_inventory_adjustment(
    *,
    agent,
    product,
    quantity_change,
    transaction_type,
    performed_by=None,
    note="",
    batch_number="",
    base_unit_cost=Decimal("0.00"),
    expires_at=None,
    received_at=None,
    reference="",
):
    if quantity_change == 0:
        raise ValidationError("Inventory adjustments must change stock by at least one unit.")
    if product.company_id != agent.company_id:
        raise ValidationError("Inventory products must belong to the same company as the agent.")

    stock, _ = AgentStock.objects.select_for_update().get_or_create(
        agent=agent,
        product=product,
        defaults={"available_quantity": 0, "reorder_level": 0},
    )
    new_quantity = stock.available_quantity + quantity_change
    if new_quantity < 0:
        raise ValidationError(f"{product.name} does not have enough stock for this adjustment.")

    batch_record = None
    if quantity_change > 0:
        received_at = received_at or timezone.localdate()
        expires_at = expires_at or (received_at + timezone.timedelta(days=365))
        batch_record = InventoryBatch.objects.create(
            agent=agent,
            product=product,
            batch_number=batch_number or f"MANUAL-{product.id}-{uuid.uuid4().hex[:8].upper()}",
            quantity_received=quantity_change,
            quantity_remaining=quantity_change,
            base_unit_cost=base_unit_cost or Decimal("0.00"),
            expires_at=expires_at,
            received_at=received_at,
        )
    else:
        _ensure_batch_sync_for_stock(agent, product, stock)
        remaining_to_remove = abs(quantity_change)
        batches = list(
            InventoryBatch.objects.select_for_update()
            .filter(agent=agent, product=product, quantity_remaining__gt=0)
            .order_by("expires_at", "received_at", "created_at")
        )
        total_available = sum(item.quantity_remaining for item in batches)
        if total_available < remaining_to_remove:
            raise ValidationError(f"{product.name} does not have enough FEFO batch stock for this adjustment.")

        for batch in batches:
            if remaining_to_remove <= 0:
                break
            deduction = min(batch.quantity_remaining, remaining_to_remove)
            batch.quantity_remaining -= deduction
            batch.save(update_fields=["quantity_remaining", "updated_at"])
            remaining_to_remove -= deduction

    stock.available_quantity = new_quantity
    stock.save(update_fields=["available_quantity", "updated_at"])

    inventory_transaction = create_inventory_transaction(
        agent=agent,
        product=product,
        transaction_type=transaction_type,
        quantity_change=quantity_change,
        stock_after=stock.available_quantity,
        performed_by=performed_by,
        batch=batch_record,
        reference=reference,
        note=note,
    )

    if stock.low_stock and agent.admin:
        notify_user(
            agent.admin,
            "Low stock alert",
            f"{product.name} is at {stock.available_quantity} units for {agent.name}.",
        )
    return inventory_transaction
