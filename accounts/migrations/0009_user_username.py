import accounts.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_user_company_admin_managed_company_consistency"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="username",
            field=models.CharField(
                blank=True,
                max_length=150,
                null=True,
                unique=True,
                validators=[accounts.validators.validate_username],
            ),
        ),
    ]
