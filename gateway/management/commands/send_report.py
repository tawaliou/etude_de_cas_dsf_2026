from django.core.management.base import BaseCommand, CommandError

from gateway.models import Sms
from gateway.operators import REPORT_STATUSES
from gateway.simulator import post_report


class Command(BaseCommand):
    help = (
        "Envoie à la passerelle (qui doit tourner) un compte rendu opérateur signé pour un SMS transmis. "
        "Permet de rejouer à la main un compte rendu en double ou à la signature fausse."
    )

    def add_arguments(self, parser):
        parser.add_argument("sms_id")
        parser.add_argument("status", choices=REPORT_STATUSES)
        parser.add_argument("--times", type=int, default=1, help="Nombre d'envois (2 = compte rendu en double).")
        parser.add_argument("--bad-signature", action="store_true", help="Signe avec une signature fausse.")

    def handle(self, sms_id, status, times, bad_signature, **options):
        sms = Sms.objects.filter(pk=sms_id).first()
        if sms is None:
            raise CommandError(f"SMS {sms_id} introuvable.")
        if not sms.operator_message_id:
            raise CommandError(f"Le SMS {sms_id} n'a pas encore été transmis (statut {sms.status}).")
        for index in range(1, times + 1):
            response = post_report(sms.operator, sms.operator_message_id, status, bad_signature)
            if response is None:
                raise CommandError("Passerelle injoignable : lancez `python manage.py runserver`.")
            self.stdout.write(f"Envoi {index} : HTTP {response.status_code} {response.text}")
