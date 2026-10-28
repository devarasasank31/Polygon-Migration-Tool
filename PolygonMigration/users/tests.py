"""Authentication flow tests (real PostgreSQL)."""

from django.test import TestCase
from django.urls import reverse

from users.models import User


def make_user(email, is_staff=False):
    return User.objects.create_user(
        email=email, password="pw", username=email.split("@")[0],
        first_name="Test", last_name="User", contact_number="", college="",
        is_staff=is_staff,
    )


class LoginTests(TestCase):
    def test_staff_login_redirects_to_index(self):
        make_user("staff@example.com", is_staff=True)
        response = self.client.post(
            reverse("login"),
            {"email": "staff@example.com", "password": "pw"},
        )
        self.assertRedirects(response, reverse("problems:index"))
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_wrong_password_shows_error(self):
        make_user("staff@example.com", is_staff=True)
        response = self.client.post(
            reverse("login"),
            {"email": "staff@example.com", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password.")

    def test_non_staff_is_rejected(self):
        make_user("student@example.com", is_staff=False)
        response = self.client.post(
            reverse("login"),
            {"email": "student@example.com", "password": "pw"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You do not have staff access.")

    def test_unknown_email_shows_error(self):
        response = self.client.post(
            reverse("login"),
            {"email": "ghost@example.com", "password": "pw"},
        )
        self.assertContains(response, "Invalid email or password.")

    def test_logout_redirects_to_login(self):
        user = make_user("staff@example.com", is_staff=True)
        self.client.force_login(user)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))


class SuperuserCommandTests(TestCase):
    def test_create_staff_superuser_from_env(self):
        from io import StringIO
        from django.core.management import call_command

        with self.settings():  # values come from os.environ
            with patch_env({
                "SUPERUSER_EMAIL": "boss@example.com",
                "SUPERUSER_PASSWORD": "s3cret-pass",
                "SUPERUSER_USERNAME": "boss",
                "SUPERUSER_FIRST_NAME": "Boss",
                "SUPERUSER_LAST_NAME": "User",
            }):
                call_command("create_staff_superuser", stdout=StringIO())

        user = User.objects.get(email="boss@example.com")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("s3cret-pass"))

        # Running again updates instead of failing
        with patch_env({
            "SUPERUSER_EMAIL": "boss@example.com",
            "SUPERUSER_PASSWORD": "new-pass",
            "SUPERUSER_USERNAME": "boss",
            "SUPERUSER_FIRST_NAME": "Boss",
            "SUPERUSER_LAST_NAME": "User",
        }):
            call_command("create_staff_superuser", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.check_password("new-pass"))
        self.assertEqual(User.objects.filter(email="boss@example.com").count(), 1)

    def test_command_requires_email_and_password(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with patch_env({}, clear_keys=["SUPERUSER_EMAIL", "SUPERUSER_PASSWORD"]):
            with self.assertRaises(CommandError):
                call_command("create_staff_superuser", stdout=StringIO())


class patch_env:
    """Context manager setting environment variables for a block."""

    def __init__(self, values, clear_keys=()):
        self.values = values
        self.clear_keys = clear_keys
        self._saved = {}

    def __enter__(self):
        import os

        for key in self.clear_keys:
            self._saved[key] = os.environ.pop(key, None)
        for key, value in self.values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False
