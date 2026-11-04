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


class Campaign(models.Model):
    """Same INFO text sent to up to 500 recipients; one Sms per accepted recipient."""

    MAX_RECIPIENTS = 500

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app_code = models.CharField(max_length=32, db_index=True)
    reference = models.CharField(max_length=100)
    text = models.TextField()
    content_hash = models.CharField(max_length=64)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    recipients_count = models.PositiveSmallIntegerField()
    accepted_count = models.PositiveSmallIntegerField()
    # Result of the submission for each recipient, in the order received:
    # {"to", "accepted", "sms_id"} or {"to", "accepted": false, "error": {"code", "message"}}
    results = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["app_code", "reference"], name="uniq_campaign_reference_per_app"),
        ]

    def __str__(self):
        return f"Campaign {self.id} [{self.app_code}/{self.reference}] {self.accepted_count}/{self.recipients_count}"

    @property
    def refused_count(self) -> int:
        return self.recipients_count - self.accepted_count


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
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.CASCADE, related_name="sms")
    # Transmission: number of attempts made, when the next one is due (scheduled
    # send or retry), and the id the operator gave to the current attempt.
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    operator_message_id = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["app_code", "reference"], name="uniq_sms_reference_per_app"),
        ]
        indexes = [
            models.Index(fields=["app_code", "quota_day"]),
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["operator", "operator_message_id"]),
        ]

    def __str__(self):
        # Never include the text: an OTP must not leak through logs or reprs.
        return f"Sms {self.id} [{self.app_code}/{self.reference}] {self.category} {self.status}"

    @property
    def public_text(self) -> str:
        return OTP_MASK if self.category == Category.OTP else self.text

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


class SmsEvent(models.Model):
    """History of an SMS: transmissions, operator answers, reports, retries.

    Details never contain the SMS text.
    """

    class Kind(models.TextChoices):
        TRANSMISSION = "TRANSMISSION", "Transmis à l'opérateur"
        DIFFERE = "DIFFERE", "Envoi différé (plage horaire des INFO)"
        OPERATEUR_INDISPONIBLE = "OPERATEUR_INDISPONIBLE", "Opérateur indisponible"
        NOUVELLE_TENTATIVE = "NOUVELLE_TENTATIVE", "Nouvelle tentative programmée"
        ECHEC = "ECHEC", "SMS échoué"
        COMPTE_RENDU = "COMPTE_RENDU", "Compte rendu pris en compte"
        COMPTE_RENDU_IGNORE = "COMPTE_RENDU_IGNORE", "Compte rendu ignoré"
        COMPTE_RENDU_REJETE = "COMPTE_RENDU_REJETE", "Compte rendu rejeté (signature invalide)"
        ANNULATION = "ANNULATION", "SMS annulé"

    sms = models.ForeignKey(Sms, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.kind} {self.detail}"
