from django.conf import settings


def deployment_settings(_request):
    return {
        'CALLS_WEBSOCKET_URL': settings.CALLS_WEBSOCKET_URL,
        'BLOB_UPLOAD_ENABLED': settings.BLOB_UPLOAD_ENABLED,
    }
