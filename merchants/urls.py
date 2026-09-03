from django.urls import path

from . import views

app_name = "merchants"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("payment-page/", views.PaymentPageView.as_view(), name="payment_page"),
    path("integrations/", views.IntegrationsView.as_view(), name="integrations"),
    path("transactions/", views.TransactionsView.as_view(), name="transactions"),
    path("transactions/export/", views.transactions_csv, name="transactions_csv"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
]
