"""
Role helpers.

Access is driven by the admin-editable ``Role`` model (per-module access
matrix + cost visibility). Legacy Django groups (Admin/Office/Warehouse/
CanSeeCosts) are still honored as a fallback so pre-existing users keep
working; ``setup_roles`` migrates group members onto Role profiles.
"""
from functools import wraps

from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied

ADMIN, OFFICE, WAREHOUSE, CAN_SEE_COSTS = "Admin", "Office", "Warehouse", "CanSeeCosts"
FULFILLMENT = "Fulfillment Manager"
ALL_GROUPS = [ADMIN, OFFICE, WAREHOUSE, CAN_SEE_COSTS]

# module sets for the seeded system roles
OFFICE_MODULES = ["dashboard", "orders", "invoices", "payments", "customers",
                  "inventory", "receiving", "shipping", "deliveries", "reports"]
WAREHOUSE_MODULES = ["picking"]
FULFILLMENT_MODULES = ["deliveries", "shipping"]
BOOKKEEPER = "Bookkeeper"
BOOKKEEPER_MODULES = ["dashboard", "invoices", "payments", "customers", "reports",
                      "expenses", "banking", "accounting"]


def _in_group(user, name):
    return user.groups.filter(name=name).exists()


def get_role(user):
    """The user's Role, or None."""
    profile = getattr(user, "profile", None)
    return profile.role if profile else None


def is_admin(user):
    if user.is_superuser or _in_group(user, ADMIN):
        return True
    role = get_role(user)
    return bool(role and role.is_admin_role)


def user_modules(user):
    """Set of module keys this user may open."""
    from core.models import MODULE_KEYS
    if is_admin(user):
        return set(MODULE_KEYS)
    role = get_role(user)
    if role:
        return set(role.modules)
    # legacy group fallback
    if _in_group(user, OFFICE):
        return set(OFFICE_MODULES)
    if _in_group(user, WAREHOUSE):
        return set(WAREHOUSE_MODULES)
    return set()


def has_module(user, module):
    return user.is_authenticated and (is_admin(user) or module in user_modules(user))


def can_see_costs(user):
    if is_admin(user) or _in_group(user, CAN_SEE_COSTS):
        return True
    role = get_role(user)
    return bool(role and role.can_see_costs)


def home_url_name(user):
    """Where this user should land: dashboard if allowed, else their first module.

    "picking" is deliberately LAST: the pick queue is a dead-end mobile shell
    with no sidebar, so it only becomes "home" when it's the user's ONLY
    module. A Fulfillment Manager who also has picking gets Deliveries as
    home, so the pick queue's home button always leads somewhere real.
    """
    from core.models import MODULE_LANDING
    mods = user_modules(user)
    if "dashboard" in mods or is_admin(user):
        return "dashboard"
    for key in ["deliveries", "orders", "shipping", "inventory",
                "invoices", "customers", "payments", "receiving", "reports",
                "expenses", "banking", "accounting", "picking"]:
        if key in mods:
            return MODULE_LANDING[key]
    return "dashboard"


def is_limited_user(user):
    """True when the user has no dashboard access (locked to their modules)."""
    return user.is_authenticated and not is_admin(user) and "dashboard" not in user_modules(user)


# Kept for existing code paths -------------------------------------------------
def is_office(user):
    return is_admin(user) or has_module(user, "orders")


def is_warehouse(user):
    return is_admin(user) or has_module(user, "picking")


def is_warehouse_only(user):
    if not user.is_authenticated or is_admin(user):
        return False
    mods = user_modules(user)
    return mods == {"picking"}


# Decorators --------------------------------------------------------------------
def admin_required(view):
    return user_passes_test(is_admin)(view)


def module_required(module):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())
            if not has_module(request.user, module):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def office_required(view):
    """Legacy name — now means 'orders module'."""
    return module_required("orders")(view)


def warehouse_required(view):
    return module_required("picking")(view)


def costs_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_see_costs(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper
