from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("<slug:uid>/", views.PayView.as_view(), name="pay"),
    path("<slug:uid>/go/<slug:provider>/", views.go, name="go"),
    path("<slug:uid>/result/<slug:ref>/", views.ResultView.as_view(), name="result"),
    path("<slug:uid>/status/<slug:ref>/", views.status, name="status"),
]
