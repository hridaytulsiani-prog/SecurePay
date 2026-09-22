from django.conf import settings
from django.http import HttpResponse
from urllib.parse import urlparse


def _is_debug_local_origin(origin):
    if not getattr(settings, "DEBUG", False):
        return False
    if not origin:
        return False

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False

    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"}


def _is_debug_tunnel_origin(origin):
    if not getattr(settings, "DEBUG", False):
        return False
    if not origin:
        return False

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False

    hostname = parsed.hostname or ""
    return parsed.scheme in {"http", "https"} and (
        hostname.endswith(".loca.lt")
        or hostname.endswith(".ngrok-free.app")
        or hostname.endswith(".ngrok-free.dev")
        or hostname.endswith(".trycloudflare.com")
    )


def _is_debug_any_browser_origin(origin):
    if not getattr(settings, "DEBUG", False):
        return False
    if not origin:
        return False

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False

    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class SimpleCORSMiddleware:
    """Minimal CORS middleware for local development origins."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin")
        is_allowed_origin = (
            origin in getattr(settings, "CORS_ALLOWED_ORIGINS", [])
            or _is_debug_local_origin(origin)
            or _is_debug_tunnel_origin(origin)
            or _is_debug_any_browser_origin(origin)
        )
        public_paths = getattr(settings, "CORS_PUBLIC_ALLOW_ALL_PATH_PREFIXES", [])
        allow_all_for_path = any(request.path.startswith(prefix) for prefix in public_paths)

        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if allow_all_for_path:
            response["Access-Control-Allow-Origin"] = "*"
            response["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Requested-With, ngrok-skip-browser-warning"
        elif is_allowed_origin:
            response["Access-Control-Allow-Origin"] = origin
            response["Vary"] = "Origin"
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Requested-With, ngrok-skip-browser-warning"

        return response
