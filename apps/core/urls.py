from django.http import HttpResponse
from django.urls import path

app_name = "core"

urlpatterns = [path("healthz/", lambda request: HttpResponse("ok", content_type="text/plain"), name="healthz")]
