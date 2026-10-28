"""Create (or update) the initial staff superuser from environment variables.

The custom user model requires several profile fields, which makes the
interactive `createsuperuser` cumbersome on a fresh setup. This command reads
the same values from the environment (see .env.example):

    SUPERUSER_EMAIL, SUPERUSER_PASSWORD, SUPERUSER_USERNAME,
    SUPERUSER_FIRST_NAME, SUPERUSER_LAST_NAME

Optionally: SUPERUSER_CONTACT_NUMBER, SUPERUSER_COLLEGE,
SUPERUSER_GRADUATION_YEAR, SUPERUSER_GENDER.

If a user with the given email already exists, the password and staff flags
are updated instead of failing.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from users.models import User

GENDER_CHOICES = {c[0] for c in User.GENDER_CHOICES}


class Command(BaseCommand):
    help = "Create or update a staff superuser from SUPERUSER_* environment variables."

    def handle(self, *args, **options):
        email = os.getenv("SUPERUSER_EMAIL", "").strip()
        password = os.getenv("SUPERUSER_PASSWORD", "")
        if not email or not password:
            raise CommandError(
                "SUPERUSER_EMAIL and SUPERUSER_PASSWORD must be set in the environment."
            )

        gender = os.getenv("SUPERUSER_GENDER", "OTHER").strip().upper() or "OTHER"
        if gender not in GENDER_CHOICES:
            raise CommandError(
                f"SUPERUSER_GENDER must be one of {sorted(GENDER_CHOICES)}."
            )

        graduation_year = os.getenv("SUPERUSER_GRADUATION_YEAR", "").strip()
        defaults = {
            "username": os.getenv("SUPERUSER_USERNAME", email.split("@")[0]),
            "first_name": os.getenv("SUPERUSER_FIRST_NAME", "Staff"),
            "last_name": os.getenv("SUPERUSER_LAST_NAME", "Admin"),
            "contact_number": os.getenv("SUPERUSER_CONTACT_NUMBER", ""),
            "college": os.getenv("SUPERUSER_COLLEGE", ""),
            "graduation_year": int(graduation_year) if graduation_year else None,
            "gender": gender,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        }

        user, created = User.objects.get_or_create(email=email, defaults=defaults)
        if not created:
            for field, value in defaults.items():
                setattr(user, field, value)
        user.set_password(password)
        user.save()

        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"Staff superuser {email} {action}."))
