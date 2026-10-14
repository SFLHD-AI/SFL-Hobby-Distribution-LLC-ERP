from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from core.csvutils import csv_response
from core.models import log_activity
from core.roles import module_required
from expenses.models import Expense, ExpenseCategory


@module_required("expenses")
def expense_list(request):
    qs = Expense.objects.filter(is_deleted=False).select_related("category")
    start, end = request.GET.get("start"), request.GET.get("end")
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    cat = request.GET.get("category")
    if cat:
        qs = qs.filter(category_id=cat)
    expenses = list(qs[:500])
    total = sum(e.amount for e in expenses)

    if request.GET.get("export") == "csv":
        return csv_response("expenses.csv",
                            ["date", "category", "vendor", "amount", "note"],
                            [[e.date, e.category or "", e.vendor, e.amount, e.note]
                             for e in expenses])

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            cat_name = request.POST.get("new_category", "").strip()
            category = None
            if cat_name:
                category, _ = ExpenseCategory.objects.get_or_create(name=cat_name)
            elif request.POST.get("category"):
                category = ExpenseCategory.objects.filter(pk=request.POST["category"]).first()
            expense = Expense.objects.create(
                date=request.POST.get("date") or None,
                category=category,
                vendor=request.POST.get("vendor", ""),
                amount=request.POST.get("amount") or 0,
                note=request.POST.get("note", ""),
                receipt_file=request.FILES.get("receipt_file"),
                created_by=request.user)
            log_activity(request.user, "create", expense)
            messages.success(request, "Expense added.")
        elif action == "del":
            expense = get_object_or_404(Expense, pk=request.POST.get("id"))
            expense.delete(user=request.user)
            messages.success(request, "Expense deleted (recoverable).")
        return redirect("expense_list")

    return render(request, "expenses/expense_list.html", {
        "expenses": expenses, "total": total,
        "categories": ExpenseCategory.objects.all(),
    })
