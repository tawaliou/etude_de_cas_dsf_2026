from django.urls import path

from . import views

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("usage", views.UsageView.as_view(), name="usage"),
    path("sms", views.SmsCollectionView.as_view(), name="sms-collection"),
    path("sms/<str:sms_id>", views.SmsDetailView.as_view(), name="sms-detail"),
    path("sms/<str:sms_id>/cancel", views.SmsCancelView.as_view(), name="sms-cancel"),
]
