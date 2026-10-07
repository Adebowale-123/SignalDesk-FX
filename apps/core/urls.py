from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("engine/tick/", views.engine_tick, name="engine_tick"),
]
