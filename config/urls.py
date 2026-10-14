from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
    path("inventory/", include("inventory.urls")),
    path("customers/", include("customers.urls")),
    path("orders/", include("orders.urls")),
    path("pick/", include("orders.pick_urls")),
    path("api/pick/", include("orders.api_urls")),
    path("invoices/", include("invoicing.urls")),
    path("payments/", include("payments.urls")),
    path("expenses/", include("expenses.urls")),
    path("shipping/", include("shipping.urls")),
    path("deliveries/", include("shipping.delivery_urls")),
    path("reports/", include("reports.urls")),
    path("banking/", include("banking.urls")),
    path("accounting/", include("accounting.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # Serve uploaded media (logo, product images, certificates, delivery
    # photos) in production too. At this scale Django serving media is fine
    # and keeps the deployment simple (no extra nginx mapping needed).
    from django.urls import re_path
    from django.views.static import serve as media_serve
    urlpatterns += [re_path(r"^media/(?P<path>.*)$", media_serve,
                            {"document_root": settings.MEDIA_ROOT})]
