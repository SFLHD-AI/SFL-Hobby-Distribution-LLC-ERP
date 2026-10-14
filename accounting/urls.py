from django.urls import path

from accounting import views

urlpatterns = [
    path("", views.index, name="accounting_index"),
    path("income-statement/", views.pnl, name="accounting_pnl"),
    path("balance-sheet/", views.balance_sheet, name="accounting_balance_sheet"),
    path("cash-vs-accrual/", views.compare, name="accounting_compare"),
    path("equity-trend/", views.equity_trend_view, name="accounting_equity_trend"),
    path("trial-balance/", views.trial_balance, name="accounting_trial_balance"),
    path("general-ledger/", views.general_ledger, name="accounting_general_ledger"),
    path("journal/", views.journal, name="accounting_journal"),
    path("accounts/", views.accounts, name="accounting_accounts"),
    path("accounts/new/", views.account_create, name="accounting_account_create"),
    path("accounts/<int:pk>/", views.account_edit, name="accounting_account_edit"),
    path("year-end/", views.year_end, name="accounting_year_end"),
    path("month-close/", views.month_close, name="accounting_month_close"),
    path("close-books/", views.period_lock, name="accounting_period_lock"),
]
