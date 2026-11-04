from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView


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
