from django.urls import path

from . import views

app_name = "trading"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("switch/", views.switch, name="switch"),
    path("test/", views.test_connection, name="test"),
    path("close-all/", views.close_all_view, name="close_all"),
    path("resume/", views.resume, name="resume"),
]
