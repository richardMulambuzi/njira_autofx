from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.login_view, name='auth-login'),
    path('register/', views.register_view, name='auth-register'),
    path('me/', views.me_view, name='auth-me'),
    path('refresh/', views.refresh_view, name='auth-refresh'),
    path('logout/', views.logout_view, name='auth-logout'),
]