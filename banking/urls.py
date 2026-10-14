from django.urls import path

from banking import views

urlpatterns = [
    # review inbox
    path("", views.inbox, name="bank_inbox"),
    path("inbox/<int:pk>/action/", views.inbox_action, name="bank_inbox_action"),
    path("inbox/bulk/", views.inbox_bulk, name="bank_inbox_bulk"),
    path("rules/run/", views.rules_run, name="bank_rules_run"),
    path("sync/", views.sync_now, name="bank_sync_now"),
    # transactions
    path("transactions/add/", views.transaction_add, name="bank_transaction_add"),
    path("transactions/<int:pk>/", views.transaction_detail, name="bank_transaction_detail"),
    # accounts
    path("accounts/", views.accounts, name="bank_accounts"),
    path("accounts/new/", views.account_create, name="bank_account_create"),
    path("accounts/<int:pk>/", views.account_detail, name="bank_account_detail"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="bank_account_edit"),
    # CSV import
    path("import/", views.bank_import, name="bank_import"),
    path("import/<int:pk>/done/", views.import_result, name="bank_import_result"),
    # rules
    path("rules/", views.rules, name="bank_rules"),
    path("rules/new/", views.rule_create, name="bank_rule_create"),
    path("rules/<int:pk>/edit/", views.rule_edit, name="bank_rule_edit"),
    path("rules/<int:pk>/delete/", views.rule_delete, name="bank_rule_delete"),
    # bank feed (Plaid)
    path("connect/", views.connect, name="bank_connect"),
    path("connect/link-token/", views.connect_link_token, name="bank_connect_link_token"),
    path("connect/exchange/", views.connect_exchange, name="bank_connect_exchange"),
    path("connect/<int:pk>/accounts/", views.connect_accounts, name="bank_connect_accounts"),
    path("connect/<int:pk>/disconnect/", views.connection_disconnect, name="bank_connection_disconnect"),
    path("connect/<int:pk>/sync/", views.connection_sync, name="bank_connection_sync"),
]
