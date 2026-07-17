from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0012_company_credit_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentBatchSaleCheckout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tx_ref", models.CharField(max_length=120, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("paid", "Paid"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("checkout_url", models.URLField(blank=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                (
                    "sale",
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        related_name="checkout",
                        to="catalog.agentbatchsale",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
    ]
