from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_user_managed_company_alter_user_phone_number"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(role="company_admin")
                        & models.Q(managed_company__isnull=False)
                    )
                    | (
                        ~models.Q(role="company_admin")
                        & models.Q(managed_company__isnull=True)
                    )
                ),
                name="user_company_admin_managed_company_consistency",
            ),
        ),
    ]
