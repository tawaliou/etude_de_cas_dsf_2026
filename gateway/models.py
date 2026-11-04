import uuid

from django.db import models

OTP_MASK = "****"


class Category(models.TextChoices):
    OTP = "OTP", "Code à usage unique"
    INFO = "INFO", "Message d'information"


class Operator(models.TextChoices):
    CELTIIS = "CELTIIS"
    MTN = "MTN"
    MOOV = "MOOV"


class SmsStatus(models.TextChoices):
    EN_ATTENTE = "EN_ATTENTE", "En attente"
    PROGRAMME = "PROGRAMME", "Programmé"
    TRANSMIS = "TRANSMIS", "Transmis"
    DELIVRE = "DELIVRE", "Délivré"
    ECHOUE = "ECHOUE", "Échoué"
    ANNULE = "ANNULE", "Annulé"


FINAL_STATUSES = {SmsStatus.DELIVRE, SmsStatus.ECHOUE, SmsStatus.ANNULE}

# Lifecycle: which statuses each status may move to. Final statuses have no exit.
ALLOWED_TRANSITIONS = {
    SmsStatus.EN_ATTENTE: {SmsStatus.TRANSMIS, SmsStatus.ECHOUE, SmsStatus.ANNULE},
    SmsStatus.PROGRAMME: {SmsStatus.EN_ATTENTE, SmsStatus.TRANSMIS, SmsStatus.ECHOUE, SmsStatus.ANNULE},
    SmsStatus.TRANSMIS: {SmsStatus.DELIVRE, SmsStatus.ECHOUE, SmsStatus.EN_ATTENTE},  # EN_ATTENTE = retry
    SmsStatus.DELIVRE: set(),
    SmsStatus.ECHOUE: set(),
    SmsStatus.ANNULE: set(),
}

CANCELLABLE_STATUSES = {SmsStatus.EN_ATTENTE, SmsStatus.PROGRAMME}


def sources_of(target: str) -> set[str]:
    """Statuses from which `target` can be reached."""
    return {source for source, targets in ALLOWED_TRANSITIONS.items() if target in targets}


class Sms(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app_code = models.CharField(max_length=32, db_index=True)
    reference = models.CharField(max_length=100)
    to = models.CharField(max_length=10)
    operator = models.CharField(max_length=16, choices=Operator.choices)
    category = models.CharField(max_length=8, choices=Category.choices)
    text = models.TextField()
    content_hash = models.CharField(max_length=64)
    segments = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16, choices=SmsStatus.choices, default=SmsStatus.EN_ATTENTE, db_index=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    # Benin calendar day the segments are charged to (day of submission).
    quota_day = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["app_code", "reference"], name="uniq_sms_reference_per_app"),
        ]
        indexes = [models.Index(fields=["app_code", "quota_day"])]

    def __str__(self):
        # Never include the text: an OTP must not leak through logs or reprs.
        return f"Sms {self.id} [{self.app_code}/{self.reference}] {self.category} {self.status}"

    @property
    def public_text(self) -> str:
        return OTP_MASK if self.category == Category.OTP else self.text

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES
