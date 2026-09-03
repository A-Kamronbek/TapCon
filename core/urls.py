from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("how-it-works/", views.HowItWorksView.as_view(), name="how_it_works"),
    path("for-business/", views.ForBusinessView.as_view(), name="for_business"),
    path("pricing/", views.PricingView.as_view(), name="pricing"),
    path("about/", views.AboutView.as_view(), name="about"),
    path("faq/", views.FaqView.as_view(), name="faq"),
    path("privacy/", views.PrivacyView.as_view(), name="privacy"),
    path("terms/", views.TermsView.as_view(), name="terms"),
    path("order/", views.order, name="order"),
    path("order/done/", views.OrderDoneView.as_view(), name="order_done"),
    path("contact/", views.contact, name="contact"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notifications_read, name="notifications_read"),
    path(
        "notifications/<int:pk>/read/",
        views.notification_read,
        name="notification_read",
    ),
    path("styleguide/", views.StyleGuideView.as_view(), name="styleguide"),
]
