"""
Simple overhead expense tracking for the month-end P&L.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import SoftDeleteModel


class ExpenseCategory(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class Expense(SoftDeleteModel):
    date = models.DateField(default=timezone.localdate, db_index=True)
    category = models.ForeignKey(ExpenseCategory, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="expenses")
    vendor = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=500, blank=True)
    receipt_file = models.FileField(upload_to="expense_receipts/", blank=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} {self.vendor} ${self.amount}"
