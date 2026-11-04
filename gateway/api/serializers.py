from rest_framework import serializers

from gateway.models import Category, Sms


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
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
