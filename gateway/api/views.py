from dataclasses import asdict

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from gateway import reports, services
from gateway.models import Campaign, Sms
from gateway.operators import SIGNATURE_HEADER

from .serializers import (
    CampaignCreateSerializer,
    CampaignListSerializer,
    CampaignSerializer,
    OperatorReportSerializer,
    ReportOutcomeSerializer,
    SmsCreateSerializer,
    SmsDetailSerializer,
    SmsFilterSerializer,
    SmsSerializer,
    UsageSerializer,
)

NOT_FOUND = OpenApiResponse(description="SMS introuvable (ou appartenant à une autre application).")
UNAUTHORIZED = OpenApiResponse(description="Clé d'API absente ou invalide.")


class SmsPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


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


class UsageView(APIView):
    @extend_schema(
        tags=["Application"],
        summary="Consommation du jour",
        description="Segments consommés aujourd'hui (heure du Bénin). Remise à zéro à minuit.",
        responses={200: UsageSerializer, 401: UNAUTHORIZED},
    )
    def get(self, request):
        return Response(UsageSerializer(services.daily_usage(request.user)).data)


class SmsCollectionView(APIView):
    @extend_schema(
        tags=["SMS"],
        summary="Lister ses SMS",
        parameters=[
            SmsFilterSerializer,
            OpenApiParameter("page", int, description="Numéro de page."),
            OpenApiParameter("page_size", int, description="Taille de page (max 200)."),
        ],
        responses={200: SmsSerializer(many=True), 400: OpenApiResponse(description="Filtre invalide."), 401: UNAUTHORIZED},
    )
    def get(self, request):
        filters = SmsFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = Sms.objects.filter(app_code=request.user.code, **filters.validated_data)
        paginator = SmsPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(SmsSerializer(page, many=True).data)

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
            401: UNAUTHORIZED,
            409: OpenApiResponse(description="Référence déjà utilisée avec un contenu différent."),
            429: OpenApiResponse(description="Quota journalier dépassé."),
        },
    )
    def post(self, request):
        payload = SmsCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        sms, created = services.submit_sms(request.user, **payload.validated_data)
        return Response(SmsSerializer(sms).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SmsDetailView(APIView):
    @extend_schema(
        tags=["SMS"],
        summary="Consulter un SMS",
        description="Statut, tentatives et historique (transmissions, comptes rendus reçus, ignorés ou rejetés).",
        responses={200: SmsDetailSerializer, 401: UNAUTHORIZED, 404: NOT_FOUND},
    )
    def get(self, request, sms_id):
        return Response(SmsDetailSerializer(services.get_app_sms(request.user, sms_id)).data)


class SmsCancelView(APIView):
    @extend_schema(
        tags=["SMS"],
        summary="Annuler un SMS",
        description="Seul un SMS en attente ou programmé (pas encore transmis) peut être annulé. "
        "Ses segments sont restitués au quota du jour.",
        request=None,
        responses={
            200: SmsSerializer,
            401: UNAUTHORIZED,
            404: NOT_FOUND,
            409: OpenApiResponse(description="SMS déjà transmis ou dans un statut définitif."),
        },
    )
    def post(self, request, sms_id):
        return Response(SmsSerializer(services.cancel_sms(request.user, sms_id)).data)


class OperatorReportView(APIView):
    """Callback used by operators. Authenticated by the HMAC signature, not by an API key."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Opérateurs"],
        summary="Compte rendu de livraison (callback opérateur)",
        description=(
            f"Corps JSON signé : en-tête `{SIGNATURE_HEADER}` = HMAC-SHA256 hexadécimal du corps brut avec le "
            "secret de l'opérateur. Signature invalide : 401, rien n'est modifié. Un compte rendu sur un SMS "
            "au statut définitif est ignoré (200, `applied: false`)."
        ),
        auth=[],
        request=OperatorReportSerializer,
        parameters=[OpenApiParameter(SIGNATURE_HEADER, str, OpenApiParameter.HEADER, required=True)],
        responses={
            200: ReportOutcomeSerializer,
            400: OpenApiResponse(description="Corps invalide."),
            401: OpenApiResponse(description="Signature invalide."),
            404: OpenApiResponse(description="Opérateur ou message inconnu."),
        },
    )
    def post(self, request, operator):
        outcome = reports.handle_report(operator.upper(), request.body, request.headers.get(SIGNATURE_HEADER))
        return Response(asdict(outcome))


class CampaignCollectionView(APIView):
    @extend_schema(
        tags=["Campagnes"],
        summary="Lister ses campagnes",
        parameters=[OpenApiParameter("page", int), OpenApiParameter("page_size", int)],
        responses={200: CampaignListSerializer(many=True), 401: UNAUTHORIZED},
    )
    def get(self, request):
        paginator = SmsPagination()
        page = paginator.paginate_queryset(Campaign.objects.filter(app_code=request.user.code), request, view=self)
        return paginator.get_paginated_response(CampaignListSerializer(page, many=True).data)

    @extend_schema(
        tags=["Campagnes"],
        summary="Soumettre une campagne",
        description=(
            "Même texte INFO envoyé à 500 destinataires au plus. Chaque destinataire est traité "
            "séparément : accepté tant que le quota le permet, refusé ensuite ; un numéro invalide ou en "
            "double est refusé. La réponse donne le résultat de chaque destinataire. Plage horaire des "
            "INFO appliquée. Même `reference` + même contenu : campagne existante (200) ; contenu "
            "différent : 409."
        ),
        request=CampaignCreateSerializer,
        responses={
            201: CampaignSerializer,
            200: OpenApiResponse(CampaignSerializer, description="Référence déjà soumise avec le même contenu."),
            400: OpenApiResponse(description="Données invalides (texte, liste vide ou > 500)."),
            401: UNAUTHORIZED,
            409: OpenApiResponse(description="Référence déjà utilisée avec un contenu différent."),
        },
    )
    def post(self, request):
        payload = CampaignCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        campaign, created = services.submit_campaign(request.user, **payload.validated_data)
        return Response(
            CampaignSerializer(campaign).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )


class CampaignDetailView(APIView):
    @extend_schema(
        tags=["Campagnes"],
        summary="Consulter une campagne",
        description="Résultat par destinataire avec le statut actuel de chaque SMS, et décompte par statut.",
        responses={200: CampaignSerializer, 401: UNAUTHORIZED, 404: OpenApiResponse(description="Campagne introuvable.")},
    )
    def get(self, request, campaign_id):
        return Response(CampaignSerializer(services.get_app_campaign(request.user, campaign_id)).data)
