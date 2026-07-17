from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0011_alter_company_admin"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="allow_agent_credit",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="company",
            name="maximum_credit_duration_days",
            field=models.PositiveIntegerField(default=14),
        ),
    ]
