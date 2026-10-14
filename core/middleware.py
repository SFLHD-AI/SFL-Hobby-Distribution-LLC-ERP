"""
Role gating: users are steered to the modules their role allows.

* Picking-only users (warehouse) are locked to the picking interface.
* Users without dashboard access who hit "/" land on their first module.
"""
from django.shortcuts import redirect
from django.urls import reverse

from core.roles import home_url_name, is_limited_user, is_warehouse_only, user_modules

ALWAYS_ALLOWED = ("/accounts", "/static", "/media", "/logout", "/django-admin")

MODULE_PREFIXES = {
    "picking": ("/pick", "/api/pick"),
    "deliveries": ("/deliveries",),
    "shipping": ("/shipping",),
    "orders": ("/orders",),
    "invoices": ("/invoices",),
    "payments": ("/payments",),
    "customers": ("/customers",),
    "inventory": ("/inventory",),
    "receiving": ("/inventory/receiving", "/inventory/cycle-counts"),
    "reports": ("/reports",),
    "expenses": ("/expenses",),
    "banking": ("/banking",),
    "accounting": ("/accounting",),
}


class RoleRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            path = request.path
            if is_warehouse_only(user):
                allowed = ("/pick", "/api/pick") + ALWAYS_ALLOWED
                if not any(path.startswith(p) for p in allowed):
                    return redirect(reverse("pick_queue"))
            elif is_limited_user(user):
                # No dashboard: redirect "/" (and anything not allowed) to
                # their first module's landing page. View decorators still
                # enforce hard permission checks.
                if path == "/":
                    return redirect(reverse(home_url_name(user)))
                mods = user_modules(user)
                allowed_prefixes = list(ALWAYS_ALLOWED)
                for m in mods:
                    allowed_prefixes += list(MODULE_PREFIXES.get(m, ()))
                if not any(path.startswith(p) for p in allowed_prefixes):
                    return redirect(reverse(home_url_name(user)))
        return self.get_response(request)
