from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0013_agentbatchsalecheckout"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="maximum_cancellation_period_minutes",
            field=models.PositiveIntegerField(default=120),
        ),
        migrations.AddField(
            model_name="company",
            name="refunds_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="CompanyRefundPolicyTier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("start_minutes", models.PositiveIntegerField()),
                ("end_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("refund_percent", models.DecimalField(decimal_places=2, max_digits=5)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="refund_policy_tiers", to="catalog.company")),
            ],
            options={
                "ordering": ("start_minutes", "id"),
                "constraints": [
                    models.UniqueConstraint(fields=("company", "start_minutes", "end_minutes"), name="unique_company_refund_policy_tier"),
                ],
            },
        ),
    ]
