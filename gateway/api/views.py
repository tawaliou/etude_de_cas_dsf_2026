from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from gateway import services
from gateway.models import Sms

from .serializers import SmsCreateSerializer, SmsFilterSerializer, SmsSerializer, UsageSerializer

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
        responses={200: SmsSerializer, 401: UNAUTHORIZED, 404: NOT_FOUND},
    )
    def get(self, request, sms_id):
        return Response(SmsSerializer(services.get_app_sms(request.user, sms_id)).data)


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
