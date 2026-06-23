from collections import defaultdict
from decimal import Decimal

import pymysql
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User, UserRole
from accounts.validators import normalize_ethiopian_phone_number
from catalog.models import (
    Agent,
    AgentStock,
    Company,
    CompanyVerificationStatus,
    Driver,
    InventoryBatch,
    InventoryTransaction,
    InventoryTransactionType,
    Product,
)
from core.models import DriverLocation


LEGACY_ROLE_MAP = {
    "customer": UserRole.CUSTOMER,
    "agent_manager": UserRole.AGENT_MANAGER,
    "agent_driver": UserRole.DRIVER,
    "driver": UserRole.DRIVER,
    "company_admin": UserRole.COMPANY_ADMIN,
    "system_admin": UserRole.SYSTEM_ADMIN,
}

INVENTORY_TRANSACTION_MAP = {
    "restock": InventoryTransactionType.RESTOCK,
    "sale": InventoryTransactionType.SALE,
    "return": InventoryTransactionType.RETURN,
    "adjustment": InventoryTransactionType.ADJUSTMENT,
}


class Command(BaseCommand):
    help = "Import users and operational seed data from the legacy MySQL schema into the current schema."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-db",
            default="water_distribution",
            help="Legacy MySQL database name to import from.",
        )
        parser.add_argument(
            "--allow-nonempty-target",
            action="store_true",
            help="Allow import even when the target database already has users or companies.",
        )

    def handle(self, *args, **options):
        source_db = options["source_db"]
        allow_nonempty_target = options["allow_nonempty_target"]

        if source_db == settings.DATABASES["default"]["NAME"]:
            raise CommandError("Source and target database names must be different.")

        if not allow_nonempty_target and (
            User.objects.exists() or Company.objects.exists() or Agent.objects.exists() or Product.objects.exists()
        ):
            raise CommandError(
                "Target database already contains users or catalog records. Use --allow-nonempty-target only if you "
                "intend to merge data carefully."
            )

        legacy = self._load_legacy_snapshot(source_db)
        with transaction.atomic():
            summary = self._import_snapshot(legacy)

        self.stdout.write(self.style.SUCCESS("Legacy import completed successfully."))
        for line in summary:
            self.stdout.write(line)

    def _legacy_connection(self, database_name):
        db_settings = settings.DATABASES["default"]
        return pymysql.connect(
            host=db_settings["HOST"] or "127.0.0.1",
            user=db_settings["USER"],
            password=db_settings["PASSWORD"],
            port=int(db_settings["PORT"] or 3306),
            database=database_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _load_legacy_snapshot(self, source_db):
        try:
            connection = self._legacy_connection(source_db)
        except Exception as exc:  # pragma: no cover - environment-specific connection failures
            raise CommandError(f"Could not connect to legacy database '{source_db}': {exc}") from exc

        tables = [
            "accounts_user",
            "companies_watercompany",
            "companies_companyadminprofile",
            "companies_agentcompany",
            "companies_agentmanagerprofile",
            "companies_driver",
            "companies_product",
            "companies_warehouse",
            "companies_inventory",
            "companies_inventorybatch",
            "companies_inventorytransaction",
        ]
        snapshot = {}
        with connection:
            with connection.cursor() as cursor:
                for table in tables:
                    cursor.execute(f"SELECT * FROM `{table}`")
                    snapshot[table] = list(cursor.fetchall())
        return snapshot

    def _import_snapshot(self, legacy):
        placeholder_emails = []
        placeholder_phones = []

        company_admin_by_company = {
            row["water_company_id"]: row["user_id"]
            for row in legacy["companies_companyadminprofile"]
            if row.get("water_company_id") and row.get("user_id")
        }
        agent_manager_by_agent = {
            row["agent_company_id"]: row["user_id"]
            for row in legacy["companies_agentmanagerprofile"]
            if row.get("agent_company_id") and row.get("user_id")
        }
        warehouse_to_agent = {
            row["id"]: row["agent_company_id"]
            for row in legacy["companies_warehouse"]
            if row.get("agent_company_id")
        }

        inventory_rows_by_id = {row["id"]: row for row in legacy["companies_inventory"]}
        product_quantity_totals = defaultdict(int)
        for row in legacy["companies_inventory"]:
            product_quantity_totals[row["product_id"]] += int(row.get("quantity") or 0)

        user_map = {}
        for old_user in legacy["accounts_user"]:
            new_user, placeholder_email, placeholder_phone = self._import_user(old_user)
            user_map[old_user["id"]] = new_user
            if placeholder_email:
                placeholder_emails.append(placeholder_email)
            if placeholder_phone:
                placeholder_phones.append(placeholder_phone)

        company_map = {}
        for old_company in legacy["companies_watercompany"]:
            admin_user = user_map.get(company_admin_by_company.get(old_company["id"]))
            company = self._import_company(old_company, admin_user=admin_user)
            company_map[old_company["id"]] = company

        product_name_counters = defaultdict(int)
        product_map = {}
        for old_product in legacy["companies_product"]:
            company = company_map.get(old_product["water_company_id"])
            if company is None:
                continue
            size_label = self._format_size_label(old_product.get("size_liters"))
            counter_key = (company.pk, (old_product.get("name") or "").strip().lower())
            product_name_counters[counter_key] += 1
            duplicate_index = product_name_counters[counter_key]
            product = self._import_product(
                old_product,
                company=company,
                available_quantity=product_quantity_totals.get(old_product["id"], 0),
                size_label=size_label,
                use_size_in_name=duplicate_index > 1,
            )
            product_map[old_product["id"]] = product

        agent_map = {}
        for old_agent in legacy["companies_agentcompany"]:
            company = company_map.get(old_agent["water_company_id"])
            if company is None:
                continue
            admin_user = user_map.get(agent_manager_by_agent.get(old_agent["id"]))
            agent = self._import_agent(old_agent, company=company, admin_user=admin_user)
            agent_map[old_agent["id"]] = agent

        for old_driver in legacy["companies_driver"]:
            user = user_map.get(old_driver.get("user_id"))
            agent = agent_map.get(old_driver.get("agent_company_id"))
            if user is None or agent is None:
                continue
            self._import_driver(old_driver, user=user, agent=agent)

        inventory_batch_map = {}
        for old_inventory in legacy["companies_inventory"]:
            agent = agent_map.get(warehouse_to_agent.get(old_inventory["warehouse_id"]))
            product = product_map.get(old_inventory["product_id"])
            if agent is None or product is None:
                continue
            AgentStock.objects.update_or_create(
                agent=agent,
                product=product,
                defaults={
                    "available_quantity": int(old_inventory.get("quantity") or 0),
                    "reorder_level": int(old_inventory.get("reorder_threshold") or 0),
                },
            )

        for old_batch in legacy["companies_inventorybatch"]:
            inventory_row = inventory_rows_by_id.get(old_batch["inventory_id"])
            if not inventory_row:
                continue
            agent = agent_map.get(warehouse_to_agent.get(inventory_row["warehouse_id"]))
            product = product_map.get(inventory_row["product_id"])
            if agent is None or product is None:
                continue
            batch = self._import_inventory_batch(old_batch, agent=agent, product=product)
            inventory_batch_map[old_batch["id"]] = batch

        for old_txn in legacy["companies_inventorytransaction"]:
            inventory_row = inventory_rows_by_id.get(old_txn["inventory_id"])
            if not inventory_row:
                continue
            agent = agent_map.get(warehouse_to_agent.get(inventory_row["warehouse_id"]))
            product = product_map.get(inventory_row["product_id"])
            if agent is None or product is None:
                continue
            self._import_inventory_transaction(
                old_txn,
                agent=agent,
                product=product,
                performed_by=user_map.get(old_txn.get("performed_by_id")),
                batch=None,
            )

        summary = [
            f"Imported {len(user_map)} users, {len(company_map)} companies, {len(agent_map)} agents, "
            f"{len(product_map)} products.",
        ]
        if placeholder_emails:
            summary.append("Generated login emails for legacy accounts without email:")
            summary.extend(f"  - {line}" for line in placeholder_emails)
        if placeholder_phones:
            summary.append("Generated placeholder phone numbers for legacy accounts missing a valid phone:")
            summary.extend(f"  - {line}" for line in placeholder_phones)
        return summary

    def _import_user(self, old_user):
        role = LEGACY_ROLE_MAP.get(old_user.get("role"), UserRole.CUSTOMER)
        first_name = (old_user.get("first_name") or "").strip()
        last_name = (old_user.get("last_name") or "").strip()
        username = (old_user.get("username") or "").strip()
        if not first_name:
            first_name = username or role.replace("_", " ").title()
        email = (old_user.get("email") or "").strip().lower()
        placeholder_email = None
        if not email:
            email = f"legacy-{old_user['id']}-{username or role}@legacy.local".replace(" ", "-")
            placeholder_email = f"{username or old_user['id']} -> {email}"

        raw_phone = old_user.get("phone")
        placeholder_phone = None
        try:
            phone_number = normalize_ethiopian_phone_number(raw_phone, required=True)
        except Exception:
            phone_number = f"+2519{old_user['id']:08d}"[-13:]
            if not phone_number.startswith("+2519"):
                phone_number = f"+2519{old_user['id']:08d}"
            placeholder_phone = f"{email} -> {phone_number}"

        date_joined = self._coerce_datetime(old_user.get("date_joined")) or timezone.now()
        last_login = self._coerce_datetime(old_user.get("last_login"))
        imported_user = User(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone_number=phone_number,
            role=role,
            is_active=bool(old_user.get("is_active")),
            is_staff=bool(old_user.get("is_staff")) or role == UserRole.SYSTEM_ADMIN,
            is_superuser=bool(old_user.get("is_superuser")) or role == UserRole.SYSTEM_ADMIN,
            is_customer=role == UserRole.CUSTOMER,
            date_joined=date_joined,
            email_verified_at=date_joined if old_user.get("is_active") else None,
            last_login=last_login,
        )
        imported_user.password = old_user.get("password") or ""
        imported_user.save(force_insert=True)
        return imported_user, placeholder_email, placeholder_phone

    def _import_company(self, old_company, *, admin_user=None):
        company = Company.objects.create(
            name=(old_company.get("name") or f"Legacy Company {old_company['id']}").strip(),
            description="Imported from the legacy bottled water database.",
            location=(old_company.get("address") or old_company.get("name") or "Imported legacy location").strip(),
            address=(old_company.get("address") or "").strip(),
            contact_email=(old_company.get("contact_email") or "").strip().lower(),
            contact_phone=self._normalize_optional_phone(old_company.get("contact_phone")),
            efda_license_number=(old_company.get("license_number") or "").strip(),
            is_active=bool(old_company.get("is_active")),
            is_verified=bool(old_company.get("is_active")),
            verification_status=(
                CompanyVerificationStatus.VERIFIED
                if old_company.get("is_active")
                else CompanyVerificationStatus.DRAFT
            ),
            admin=admin_user if admin_user and admin_user.role == UserRole.COMPANY_ADMIN else None,
        )
        if admin_user and admin_user.role == UserRole.COMPANY_ADMIN:
            admin_user.managed_company = company
            admin_user.save(update_fields=["managed_company", "updated_at"])
        return company

    def _import_product(self, old_product, *, company, available_quantity, size_label, use_size_in_name):
        base_name = (old_product.get("name") or "Imported Product").strip()
        product_name = f"{base_name} {size_label}".strip() if use_size_in_name and size_label else base_name
        return Product.objects.create(
            company=company,
            name=product_name,
            size_label=size_label,
            description=(old_product.get("description") or "Imported from the legacy product catalog.").strip(),
            price=Decimal(str(old_product.get("price") or "0")),
            available_quantity=max(int(available_quantity or 0), 0),
            is_active=bool(old_product.get("is_active")),
        )

    def _import_agent(self, old_agent, *, company, admin_user=None):
        return Agent.objects.create(
            company=company,
            name=(old_agent.get("name") or f"{company.name} Agent").strip(),
            description="Imported from the legacy agent branch records.",
            location_name=(old_agent.get("address") or old_agent.get("name") or company.location).strip(),
            address=(old_agent.get("address") or "").strip(),
            latitude=old_agent.get("warehouse_latitude"),
            longitude=old_agent.get("warehouse_longitude"),
            service_radius_km=Decimal(str(old_agent.get("service_radius_km") or "15")),
            phone_number=self._normalize_optional_phone(old_agent.get("contact_phone")),
            is_active=bool(old_agent.get("is_active")),
            is_accepting_orders=bool(old_agent.get("is_active")),
            credit_limit=Decimal("0"),
            credit_period_days=14,
            admin=admin_user if admin_user and admin_user.role == UserRole.AGENT_MANAGER else None,
        )

    def _import_driver(self, old_driver, *, user, agent):
        driver = Driver.objects.create(
            agent=agent,
            user=user,
            vehicle_identifier=(old_driver.get("vehicle_type") or "").strip(),
            phone_number=user.phone_number,
            is_active=bool(user.is_active),
            availability_status=(
                Driver.AvailabilityStatus.AVAILABLE
                if old_driver.get("is_available")
                else Driver.AvailabilityStatus.OFF_DUTY
            ),
        )
        if old_driver.get("current_latitude") is not None and old_driver.get("current_longitude") is not None:
            DriverLocation.objects.update_or_create(
                driver_user=user,
                defaults={
                    "latitude": old_driver["current_latitude"],
                    "longitude": old_driver["current_longitude"],
                    "is_online": bool(old_driver.get("is_available")),
                },
            )
        return driver

    def _import_inventory_batch(self, old_batch, *, agent, product):
        batch_number = (old_batch.get("batch_reference") or f"LEGACY-BATCH-{old_batch['id']}").strip()
        quantity_remaining = max(int(old_batch.get("quantity_remaining") or 0), 0)
        return InventoryBatch.objects.create(
            agent=agent,
            product=product,
            batch_number=batch_number,
            quantity_received=quantity_remaining,
            quantity_remaining=quantity_remaining,
            base_unit_cost=Decimal(str(old_batch.get("cost_per_unit") or "0")),
            expires_at=old_batch.get("expiry_date") or timezone.localdate(),
            received_at=(old_batch.get("received_at") or timezone.now()).date(),
        )

    def _import_inventory_transaction(self, old_txn, *, agent, product, performed_by=None, batch=None):
        transaction_type = INVENTORY_TRANSACTION_MAP.get(
            (old_txn.get("transaction_type") or "").strip().lower(),
            InventoryTransactionType.ADJUSTMENT,
        )
        InventoryTransaction.objects.create(
            agent=agent,
            product=product,
            batch=batch,
            performed_by=performed_by,
            transaction_type=transaction_type,
            quantity_change=int(old_txn.get("quantity_changed") or 0),
            stock_after=max(int(old_txn.get("quantity_after") or 0), 0),
            reference=str(old_txn.get("reference_id") or ""),
            note=(old_txn.get("note") or "").strip(),
        )

    def _format_size_label(self, size_value):
        if size_value in (None, ""):
            return ""
        size = Decimal(str(size_value)).normalize()
        return f"{size}L"

    def _normalize_optional_phone(self, value):
        try:
            return normalize_ethiopian_phone_number(value, required=False)
        except Exception:
            return ""

    def _coerce_datetime(self, value):
        if value is None:
            return None
        if timezone.is_naive(value):
            return timezone.make_aware(value, timezone.get_current_timezone())
        return value
