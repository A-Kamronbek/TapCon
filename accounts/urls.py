from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("verify/", views.verify, name="verify"),
    path("resend/", views.resend, name="resend"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("password-reset/", views.password_reset, name="password_reset"),
    path("password-reset/new/", views.password_reset_set, name="password_reset_set"),
]
