from django.urls import path

from . import views

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("sms", views.SmsCollectionView.as_view(), name="sms-collection"),
]
