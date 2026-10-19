"""The signal-card mailer must send each card once, and only once."""
import os
import tempfile
from unittest import mock

import django

# This repo has no pytest-django config, so nothing sets the app registry up
# before collection. Without this, importing the command (which pulls in DRF
# via notifications.views) raises AppRegistryNotReady.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import TestCase  # noqa: E402

SEND_TARGET = "forex.management.commands.email_signal_cards.send_email"


def _make_card(cards_dir):
    month = os.path.join(cards_dir, "2026-10")
    os.makedirs(month, exist_ok=True)
    card = os.path.join(month, "XAUUSD_BUY_20261019_120000_abcd1234.md")
    with open(card, "w") as handle:
        handle.write("# Signal: XAUUSD BUY\n")
    return card


class EmailSignalCardsTests(TestCase):
    def test_each_card_is_emailed_once(self):
        with tempfile.TemporaryDirectory() as cards_dir:
            card = _make_card(cards_dir)

            with mock.patch(SEND_TARGET, return_value={"success": True}) as send:
                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"])
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.kwargs["attachment"], card)

                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"])
                self.assertEqual(send.call_count, 1, "a second run must not resend")

    def test_all_flag_resends_every_card(self):
        with tempfile.TemporaryDirectory() as cards_dir:
            _make_card(cards_dir)

            with mock.patch(SEND_TARGET, return_value={"success": True}) as send:
                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"])
                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"], all=True)
                self.assertEqual(send.call_count, 2)

    def test_failure_is_not_recorded_so_it_retries(self):
        with tempfile.TemporaryDirectory() as cards_dir:
            _make_card(cards_dir)

            failed = {"success": False, "message": "smtp down"}
            with mock.patch(SEND_TARGET, return_value=failed) as send:
                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"])
                call_command("email_signal_cards", dir=cards_dir, to=["a@example.com"])
                self.assertEqual(send.call_count, 2, "a failed send must be retried")
