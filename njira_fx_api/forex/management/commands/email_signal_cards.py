"""Email every signal card that has not been emailed yet.

The engine writes one markdown card per generated signal (see
``forex.analysis.signal_logger``). This command walks those cards, skips the
ones already sent, and mails the rest with the card attached. Card paths that
went out are recorded in a small JSON state file, so a timer can run this
repeatedly without spamming the same signal.
"""
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from forex.analysis import signal_logger
from notifications.views import send_email

STATE_FILENAME = ".emailed.json"


def _state_path(directory):
    return os.path.join(directory, STATE_FILENAME)


def _load_sent(directory):
    try:
        with open(_state_path(directory)) as handle:
            return set(json.load(handle))
    except (OSError, ValueError):
        return set()


def _save_sent(directory, sent):
    tmp = _state_path(directory) + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(sorted(sent), handle)
    os.replace(tmp, _state_path(directory))


def _iter_cards(directory):
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.endswith(".md"):
                yield os.path.join(root, name)


class Command(BaseCommand):
    help = "Email signal cards that have not been emailed yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            default=signal_logger.SIGNAL_LOG_DIR,
            help="Directory holding the signal cards.",
        )
        parser.add_argument(
            "--to",
            action="append",
            default=None,
            help="Recipient. Repeatable. Defaults to settings.SIGNAL_EMAIL_TO.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent, then exit.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Ignore the sent state file and email every card again.",
        )

    def handle(self, *args, **options):
        directory = options["dir"]
        recipients = options["to"] or list(getattr(settings, "SIGNAL_EMAIL_TO", []) or [])
        if not recipients:
            self.stderr.write("No recipients. Pass --to or set SIGNAL_EMAIL_TO.")
            return

        if not os.path.isdir(directory):
            self.stdout.write("No card directory at %s, nothing to send." % directory)
            return

        sent = set() if options["all"] else _load_sent(directory)
        cards = list(_iter_cards(directory))
        pending = [card for card in cards if card not in sent]
        self.stdout.write("%d card(s) on disk, %d pending." % (len(cards), len(pending)))

        if options["dry_run"]:
            for card in pending:
                self.stdout.write("  would email %s" % card)
            return

        delivered = 0
        for card in pending:
            with open(card) as handle:
                body = handle.read()
            subject = "njira signal: " + os.path.basename(card)[:-3]
            ok = True
            for address in recipients:
                result = send_email(address, subject, body, attachment=card)
                if not result.get("success"):
                    ok = False
                    self.stderr.write("FAILED %s for %s: %s" % (card, address, result.get("message")))
            if ok:
                sent.add(card)
                delivered += 1

        _save_sent(directory, sent)
        self.stdout.write(
            self.style.SUCCESS("Emailed %d card(s) to %s." % (delivered, ", ".join(recipients)))
        )
