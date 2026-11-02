"""Loopback-only settings for checking the supported Nautobot runtime."""

from nautobot.core.settings import *  # noqa: F401,F403

# Fixed value for loopback-only disposable tests, never a deployment credential.
SECRET_KEY = "development-only-runtime-compatibility-check"  # nosec B105
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
PLUGINS = ["nautobot_ssot", "nautobot_ssot_unifi"]
PLUGINS_CONFIG = {"nautobot_ssot": {}, "nautobot_ssot_unifi": {}}
INSTALLATION_METRICS_ENABLED = False
METRICS_ENABLED = False
CONSTANCE_BACKEND = "constance.backends.memory.MemoryBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
