from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("catalog", "0010_agent_overdue_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="company",
            name="admin",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="primary_managed_company",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
