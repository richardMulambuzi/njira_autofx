"""The console gate: every page refuses anonymous traffic, and a correct
password attaches a real principal so the IsAuthenticated API answers."""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")

import django  # noqa: E402
from django.test import Client, TestCase, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402

django.setup()

PASSWORD = "test-console-password"
PAGES = ["dashboard", "console-markets", "console-signals", "console-strategies", "console-system"]


@override_settings(ALLOWED_HOSTS=["testserver"])
class ConsoleGateTests(TestCase):
    def setUp(self):
        os.environ["NJIRA_DASHBOARD_PASSWORD"] = PASSWORD
        self.addCleanup(os.environ.pop, "NJIRA_DASHBOARD_PASSWORD", None)
        self.client = Client()

    def test_every_page_refuses_anonymous_and_renders_the_form(self):
        for name in PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, 'name="password"')

    def test_the_login_form_on_any_page_accepts_the_password(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.client.post(reverse("dashboard-logout"))
                resp = self.client.post(reverse(name), {"password": PASSWORD}, follow=True)
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Console sections")

    def test_wrong_password_is_rejected(self):
        resp = self.client.post(reverse("dashboard"), {"password": "definitely-not-it"})
        self.assertContains(resp, "Wrong password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_password_signs_in_and_attaches_a_real_user(self):
        resp = self.client.post(reverse("dashboard"), {"password": PASSWORD})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Console sections")
        self.assertTrue(self.client.session.get("njira_dashboard_ok"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_signed_in_session_reaches_a_jwt_gated_api(self):
        self.client.post(reverse("dashboard"), {"password": PASSWORD})
        resp = self.client.get("/api/provider/status/")
        self.assertNotEqual(resp.status_code, 401)

    def test_signed_in_pages_render_the_nav(self):
        self.client.post(reverse("dashboard"), {"password": PASSWORD})
        for name in PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Console sections")

    def test_live_endpoint_still_refuses_an_anonymous_session(self):
        self.assertEqual(self.client.get(reverse("dashboard-live")).status_code, 403)

    def test_without_a_configured_password_access_is_refused(self):
        os.environ.pop("NJIRA_DASHBOARD_PASSWORD", None)
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "access is refused")

    def test_logout_drops_both_the_flag_and_the_user(self):
        self.client.post(reverse("dashboard"), {"password": PASSWORD})
        self.client.post(reverse("dashboard-logout"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard-live")).status_code, 403)
