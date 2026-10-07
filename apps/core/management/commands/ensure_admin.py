import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if it doesn't exist (used on deploy)."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not username or not password:
            self.stdout.write("ADMIN_USERNAME / ADMIN_PASSWORD not set; skipping admin creation.")
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin {username} already exists; leaving it unchanged.")
            return
        User.objects.create_superuser(username, os.environ.get("ADMIN_EMAIL", ""), password)
        self.stdout.write(self.style.SUCCESS(f"Created admin {username}."))
