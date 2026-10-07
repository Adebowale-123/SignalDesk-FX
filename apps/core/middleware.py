from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

OPEN_PATHS = ("/login/", "/admin/", "/static/", "/healthz/", "/engine/tick/")


class LoginRequiredMiddleware:
    """Private portal: every page needs a login (admin has its own login)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(OPEN_PATHS):
            return redirect(f"{reverse(settings.LOGIN_URL)}?next={request.path}")
        return self.get_response(request)


class EngineOnRequestMiddleware:
    """When there is no background worker, refresh the analysis on visits (at most every few minutes)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.ENGINE_ON_REQUEST and not request.path.startswith(("/static/", "/healthz/")):
            from django.core.cache import cache

            from apps.core.models import SiteSettings

            minutes = SiteSettings.load().engine_interval_minutes
            if cache.add("engine:last-run", True, max(60, minutes * 60)):
                start_engine()
        return self.get_response(request)


def start_engine():
    """Run one analysis cycle in the background (skipped if one is already running)."""
    from apps.signals.services import run_cycle

    from . import background

    return background.start("engine", run_cycle)
