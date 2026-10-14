from django.urls import path

from core import users_views, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_edit, name="settings_edit"),
    path("activity/", views.activity_log, name="activity_log"),
    path("search/", views.global_search, name="global_search"),

    # Users & roles (admin only)
    path("users/", users_views.user_list, name="user_list"),
    path("users/new/", users_views.user_create, name="user_create"),
    path("users/<int:pk>/", users_views.user_edit, name="user_edit"),
    path("users/roles/", users_views.role_list, name="role_list"),
    path("users/roles/new/", users_views.role_create, name="role_create"),
    path("users/roles/<int:pk>/", users_views.role_edit, name="role_edit"),
    path("users/roles/<int:pk>/delete/", users_views.role_delete, name="role_delete"),
]
