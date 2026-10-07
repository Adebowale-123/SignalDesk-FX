import hmac

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden

from apps.core.models import SiteSettings

from .middleware import start_engine


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def engine_tick(request):
    """Called by an external scheduler (e.g. cron-job.org) every few minutes to keep the analysis running.

    Needs ?key=ENGINE_TICK_KEY. Also keeps a free Render service awake.
    """
    key = settings.ENGINE_TICK_KEY
    if not key or not hmac.compare_digest(request.GET.get("key", ""), key):
        return HttpResponseForbidden("bad key")
    minutes = SiteSettings.load().engine_interval_minutes
    if cache.add("engine:last-run", True, max(60, minutes * 60 - 30)):
        started = start_engine()
        return HttpResponse("started" if started else "already running", content_type="text/plain")
    return HttpResponse("ok, ran recently", content_type="text/plain")
