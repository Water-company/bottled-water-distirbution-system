from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0007_order_orders_order_status_idx"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Complaint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reference_number", models.CharField(editable=False, max_length=24, unique=True)),
                ("category", models.CharField(choices=[("missing_items", "Missing items"), ("incorrect_quantity", "Incorrect quantity"), ("wrong_products", "Wrong products"), ("damaged_products", "Damaged products"), ("poor_quality", "Poor product quality"), ("other", "Other issues")], max_length=30)),
                ("description", models.TextField()),
                ("status", models.CharField(choices=[("pending_review", "Pending Review"), ("under_investigation", "Under Investigation"), ("approved", "Approved"), ("rejected", "Rejected"), ("resolved", "Resolved")], default="pending_review", max_length=30)),
                ("resolution_type", models.CharField(blank=True, choices=[("full_refund", "Full refund"), ("partial_refund", "Partial refund"), ("replacement_delivery", "Replacement delivery"), ("additional_delivery", "Additional delivery for missing products"), ("rejected", "Complaint rejected")], max_length=30)),
                ("resolution_note", models.TextField(blank=True)),
                ("refund_amount", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("agent_note", models.TextField(blank=True)),
                ("final_decision_reason", models.TextField(blank=True)),
                ("agent_reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("final_reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("agent_reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agent_reviewed_complaints", to=settings.AUTH_USER_MODEL)),
                ("final_reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="final_reviewed_complaints", to=settings.AUTH_USER_MODEL)),
                ("linked_refund_request", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="complaint", to="orders.refundrequest")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="complaints", to="orders.order")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="submitted_complaints", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="ComplaintEvidence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("image", models.ImageField(upload_to="orders/complaints/")),
                ("complaint", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="evidences", to="orders.complaint")),
            ],
            options={
                "ordering": ("created_at",),
            },
        ),
        migrations.CreateModel(
            name="RefundTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.CharField(choices=[("chapa", "Chapa")], default="chapa", max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="pending", max_length=20)),
                ("provider_reference", models.CharField(blank=True, max_length=120)),
                ("request_payload", models.JSONField(blank=True, default=dict)),
                ("response_payload", models.JSONField(blank=True, default=dict)),
                ("failure_reason", models.TextField(blank=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("complaint", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="refund_transactions", to="orders.complaint")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="refund_transactions", to="orders.payment")),
                ("processed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recorded_refund_transactions", to=settings.AUTH_USER_MODEL)),
                ("refund_request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="transactions", to="orders.refundrequest")),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="RefundRequestStatusHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending Review"), ("approved", "Approved"), ("rejected", "Rejected"), ("processed", "Processed"), ("failed", "Failed")], max_length=20)),
                ("title", models.CharField(max_length=80)),
                ("note", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="refund_status_updates", to=settings.AUTH_USER_MODEL)),
                ("refund_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="status_history", to="orders.refundrequest")),
            ],
            options={
                "verbose_name_plural": "refund request status history",
                "ordering": ("created_at",),
            },
        ),
        migrations.CreateModel(
            name="ComplaintStatusHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending_review", "Pending Review"), ("under_investigation", "Under Investigation"), ("approved", "Approved"), ("rejected", "Rejected"), ("resolved", "Resolved")], max_length=30)),
                ("title", models.CharField(max_length=80)),
                ("note", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="complaint_status_updates", to=settings.AUTH_USER_MODEL)),
                ("complaint", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="status_history", to="orders.complaint")),
            ],
            options={
                "verbose_name_plural": "complaint status history",
                "ordering": ("created_at",),
            },
        ),
        migrations.CreateModel(
            name="SupportActionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.CharField(max_length=80)),
                ("details", models.TextField(blank=True)),
                ("outcome", models.CharField(blank=True, max_length=80)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="support_action_logs", to=settings.AUTH_USER_MODEL)),
                ("complaint", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="support_logs", to="orders.complaint")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="support_action_logs", to="orders.order")),
                ("refund_request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="support_logs", to="orders.refundrequest")),
            ],
            options={
                "ordering": ("created_at",),
            },
        ),
    ]
