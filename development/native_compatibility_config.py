"""Loopback-only settings for the disposable native compatibility CI services."""

from development.compatibility_config import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "unifi_namespace_check",
        "USER": "nautobot",
        # Matches the disposable loopback database service in CI.
        "PASSWORD": "disposable-test-only",  # nosec B105
        "HOST": "127.0.0.1",
        "PORT": "5432",
    }
}
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/0",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
