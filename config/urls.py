from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.trading import views as trading_views

admin.site.site_header = "SignalDesk FX"
admin.site.site_title = "SignalDesk FX"
admin.site.index_title = "Administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("apps.core.urls")),
    path("auto-trading/", include("apps.trading.urls")),
    path("api/bridge/", trading_views.bridge_api, name="bridge_api"),
    path("", include("apps.signals.urls")),
]
