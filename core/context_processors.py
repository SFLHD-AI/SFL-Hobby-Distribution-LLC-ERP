from core.models import CompanySettings
from core.version import APP_VERSION
from core.roles import (can_see_costs, home_url_name, is_admin, is_office,
                        is_warehouse_only, user_modules)


def company(request):
    ctx = {"company": CompanySettings.get(), "app_version": APP_VERSION}
    if request.user.is_authenticated:
        ctx.update({
            "u_is_admin": is_admin(request.user),
            "u_is_office": is_office(request.user),
            "u_warehouse_only": is_warehouse_only(request.user),
            "u_can_see_costs": can_see_costs(request.user),
            "u_modules": user_modules(request.user),
            "u_home_url": home_url_name(request.user),
        })
    return ctx
