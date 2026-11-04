from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from gateway import services

from .serializers import SmsCreateSerializer, SmsSerializer


class MeView(APIView):
    """Identify the calling application (useful to check an API key)."""

    @extend_schema(
        tags=["Application"],
        summary="Application appelante",
        responses=inline_serializer(
            "Me", {"code": serializers.CharField(), "daily_quota": serializers.IntegerField()}
        ),
    )
    def get(self, request):
        app = request.user
        return Response({"code": app.code, "daily_quota": app.daily_quota})


class SmsCollectionView(APIView):
    @extend_schema(
        tags=["SMS"],
        summary="Soumettre un SMS",
        description=(
            "Valide le numéro (10 chiffres, commence par 01, opérateur connu), compte les segments "
            "(max 6) et vérifie le quota du jour. Une même `reference` avec le même contenu renvoie "
            "le SMS existant (200) ; avec un contenu différent, elle est refusée (409)."
        ),
        request=SmsCreateSerializer,
        responses={
            201: SmsSerializer,
            200: OpenApiResponse(SmsSerializer, description="Référence déjà soumise avec le même contenu."),
            400: OpenApiResponse(description="Données invalides."),
            401: OpenApiResponse(description="Clé d'API absente ou invalide."),
            409: OpenApiResponse(description="Référence déjà utilisée avec un contenu différent."),
            429: OpenApiResponse(description="Quota journalier dépassé."),
        },
    )
    def post(self, request):
        payload = SmsCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        sms, created = services.submit_sms(request.user, **payload.validated_data)
        return Response(SmsSerializer(sms).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
