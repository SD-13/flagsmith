import django

if django.VERSION < (3, 2):
    default_app_config = "integrations.sentry.apps.SentryConfig"
