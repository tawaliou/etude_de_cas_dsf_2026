import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from gateway import dispatch

logger = logging.getLogger("gateway.worker")


class Command(BaseCommand):
    help = "Transmet en continu les SMS dus aux opérateurs (envois, programmations, nouvelles tentatives)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Un seul passage puis arrêt.")
        parser.add_argument(
            "--interval", type=float, default=settings.WORKER_INTERVAL_SECONDS, help="Secondes entre deux passages."
        )

    def handle(self, *args, once=False, interval=1.0, **options):
        self.stdout.write(self.style.SUCCESS(f"Worker démarré (intervalle {interval}s). Ctrl+C pour arrêter."))
        try:
            while True:
                try:
                    count = dispatch.dispatch_due()
                    if count:
                        logger.info("worker: %s SMS handed to operators", count)
                except Exception:  # keep the worker alive, the next pass retries
                    logger.exception("worker pass failed")
                if once:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Worker arrêté.")
