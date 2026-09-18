"""Loopback-only settings for checking the supported Nautobot runtime."""

from nautobot.core.settings import *  # noqa: F403

SECRET_KEY = "development-only-runtime-compatibility-check"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
PLUGINS = ["nautobot_ssot", "nautobot_ssot_unifi"]
PLUGINS_CONFIG = {"nautobot_ssot": {}, "nautobot_ssot_unifi": {}}
INSTALLATION_METRICS_ENABLED = False
METRICS_ENABLED = False
CONSTANCE_BACKEND = "constance.backends.memory.MemoryBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
