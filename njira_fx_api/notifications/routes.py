from django.urls import path
from . import views

urlpatterns = [
    path('sendemail/', views.send_email_api, name='send_email_api'),
    path('sendsampleemail/', views.dummy_send_email, name='dummy_send_email'),
    path('fetch/', views.fetch_notifications_by_email, name='fetch_notifications_by_email'),
    path('delete/', views.delete_notifications_by_email, name='delete_notifications_by_email'),
    path("delete-by-id/", views.delete_notification_by_id, name='delete_notification_by_id'),
]
