from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("", RedirectView.as_view(url="/api/docs", permanent=False)),
    path("api/", include("gateway.api.urls")),
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]


def json_404(request, exception=None):
    return JsonResponse({"error": {"code": "not_found", "message": "Ressource introuvable."}}, status=404)


def json_500(request):
    return JsonResponse({"error": {"code": "server_error", "message": "Erreur interne."}}, status=500)


handler404 = json_404
handler500 = json_500
