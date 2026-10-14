from django.urls import path

from orders import api

urlpatterns = [
    path("queue/", api.pick_queue_api, name="api_pick_queue"),
    path("po/<int:pk>/", api.pick_detail_api, name="api_pick_detail"),
    path("po/<int:pk>/scan/", api.pick_scan_api, name="api_pick_scan"),
    path("po/<int:pk>/complete/", api.pick_complete_api, name="api_pick_complete"),
    path("line/<int:line_id>/adjust/", api.pick_adjust_api, name="api_pick_adjust"),
    path("line/<int:line_id>/set/", api.pick_set_qty_api, name="api_pick_set_qty"),
    path("line/<int:line_id>/short/", api.pick_short_api, name="api_pick_short"),
]
