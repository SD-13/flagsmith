import django

if django.VERSION < (3, 2):
    default_app_config = "integrations.new_relic.apps.NewRelicConfigurationConfig"
