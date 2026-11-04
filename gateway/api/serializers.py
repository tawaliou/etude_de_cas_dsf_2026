from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from gateway.models import Campaign, Category, Sms, SmsEvent, SmsStatus


class SmsFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SmsStatus.choices, required=False)
    category = serializers.ChoiceField(choices=Category.choices, required=False)
    reference = serializers.CharField(required=False)


class UsageSerializer(serializers.Serializer):
    date = serializers.DateField(help_text="Jour courant, heure du Bénin.")
    quota = serializers.IntegerField(help_text="Quota journalier en segments.")
    used = serializers.IntegerField(help_text="Segments consommés (SMS annulés exclus).")
    remaining = serializers.IntegerField()


class SmsCreateSerializer(serializers.Serializer):
    to = serializers.CharField(max_length=20, help_text="Numéro du destinataire, ex. 0197000000.")
    text = serializers.CharField(max_length=2000, trim_whitespace=False)
    category = serializers.ChoiceField(choices=Category.choices)
    reference = serializers.CharField(max_length=100, help_text="Référence propre à l'application (envoi unique).")
    send_at = serializers.DateTimeField(
        required=False, allow_null=True, help_text="Envoi programmé (ISO 8601). Passé ou absent : envoi immédiat."
    )


class SmsSerializer(serializers.ModelSerializer):
    text = serializers.CharField(source="public_text", help_text="Masqué (****) pour un OTP.")

    class Meta:
        model = Sms
        fields = [
            "id",
            "reference",
            "to",
            "operator",
            "category",
            "text",
            "segments",
            "status",
            "scheduled_at",
            "attempts",
            "next_attempt_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class SmsEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SmsEvent
        fields = ["kind", "detail", "created_at"]


class SmsDetailSerializer(SmsSerializer):
    history = SmsEventSerializer(source="events", many=True, read_only=True)

    class Meta(SmsSerializer.Meta):
        fields = [*SmsSerializer.Meta.fields, "operator_message_id", "history"]
        read_only_fields = fields


class OperatorReportSerializer(serializers.Serializer):
    """Documentation only: the raw body is what gets signed and verified."""

    message_id = serializers.CharField(help_text="Identifiant donné par l'opérateur à la transmission.")
    status = serializers.ChoiceField(choices=["DELIVRE", "ECHEC_TEMPORAIRE", "ECHEC_DEFINITIF"])
    timestamp = serializers.DateTimeField(required=False)


class ReportOutcomeSerializer(serializers.Serializer):
    applied = serializers.BooleanField(help_text="False si le compte rendu a été ignoré (statut déjà définitif).")
    sms_id = serializers.CharField()
    sms_status = serializers.CharField()
    detail = serializers.CharField()


class CampaignCreateSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=100, help_text="Référence propre à l'application (envoi unique).")
    text = serializers.CharField(max_length=2000, trim_whitespace=False, help_text="Texte INFO commun.")
    recipients = serializers.ListField(
        child=serializers.CharField(max_length=20),
        min_length=1,
        max_length=Campaign.MAX_RECIPIENTS,
        help_text=f"Numéros des destinataires ({Campaign.MAX_RECIPIENTS} au plus).",
    )
    send_at = serializers.DateTimeField(required=False, allow_null=True)


class RecipientErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class RecipientResultSerializer(serializers.Serializer):
    to = serializers.CharField()
    accepted = serializers.BooleanField()
    sms_id = serializers.CharField(required=False, help_text="Si accepté.")
    status = serializers.CharField(required=False, help_text="Statut actuel du SMS, si accepté.")
    error = RecipientErrorSerializer(required=False, help_text="Motif du refus, sinon.")


class CampaignSerializer(serializers.ModelSerializer):
    refused_count = serializers.IntegerField(read_only=True)
    status_summary = serializers.SerializerMethodField(help_text="Nombre de SMS par statut actuel.")
    results = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = [
            "id",
            "reference",
            "text",
            "scheduled_at",
            "recipients_count",
            "accepted_count",
            "refused_count",
            "status_summary",
            "results",
            "created_at",
        ]
        read_only_fields = fields

    def _current_statuses(self, campaign) -> dict[str, str]:
        if not hasattr(campaign, "_statuses"):
            campaign._statuses = {str(pk): status for pk, status in campaign.sms.values_list("pk", "status")}
        return campaign._statuses

    def get_status_summary(self, campaign) -> dict[str, int]:
        summary: dict[str, int] = {}
        for status in self._current_statuses(campaign).values():
            summary[status] = summary.get(status, 0) + 1
        return summary

    @extend_schema_field(RecipientResultSerializer(many=True))
    def get_results(self, campaign) -> list[dict]:
        statuses = self._current_statuses(campaign)
        return [
            {**result, "status": statuses.get(result["sms_id"])} if result.get("accepted") else result
            for result in campaign.results
        ]


class CampaignListSerializer(CampaignSerializer):
    class Meta(CampaignSerializer.Meta):
        fields = [f for f in CampaignSerializer.Meta.fields if f != "results"]
        read_only_fields = fields
