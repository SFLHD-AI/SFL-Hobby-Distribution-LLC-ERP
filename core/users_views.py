"""
Admin-only Users module: create/edit accounts, assign roles, reset passwords,
and manage the roles themselves (per-module access matrix).
"""
from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render

from core.models import MODULES, Role, UserProfile, log_activity
from core.roles import admin_required


class UserForm(forms.Form):
    username = forms.CharField(max_length=150)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ModelChoiceField(queryset=Role.objects.all())
    phone = forms.CharField(max_length=50, required=False)
    password = forms.CharField(widget=forms.PasswordInput, required=False,
                               help_text="Required for new users; leave blank to keep unchanged")
    is_active = forms.BooleanField(required=False, initial=True)


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ["name", "can_see_costs", "is_admin_role"]


@admin_required
def user_list(request):
    users = (User.objects.select_related("profile", "profile__role")
             .order_by("-is_active", "username"))
    return render(request, "core/user_list.html", {"users": users})


@admin_required
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if User.objects.filter(username__iexact=data["username"]).exists():
            form.add_error("username", "That username is taken.")
        elif not data["password"]:
            form.add_error("password", "Set a password for the new user.")
        else:
            user = User.objects.create_user(
                username=data["username"], email=data["email"],
                password=data["password"], first_name=data["first_name"],
                last_name=data["last_name"], is_active=data["is_active"])
            UserProfile.objects.create(user=user, role=data["role"],
                                       phone=data["phone"])
            log_activity(request.user, "user_created", user,
                         note=f"role: {data['role'].name}")
            messages.success(request, f"User {user.username} created with role "
                                      f"“{data['role'].name}”.")
            return redirect("user_list")
    return render(request, "core/user_form.html", {"form": form, "edit_user": None})


@admin_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    initial = {"username": user.username, "first_name": user.first_name,
               "last_name": user.last_name, "email": user.email,
               "role": profile.role, "phone": profile.phone,
               "is_active": user.is_active}
    form = UserForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        clash = User.objects.filter(username__iexact=data["username"]).exclude(pk=user.pk)
        if clash.exists():
            form.add_error("username", "That username is taken.")
        else:
            user.username = data["username"]
            user.first_name, user.last_name = data["first_name"], data["last_name"]
            user.email = data["email"]
            if user.pk != request.user.pk:  # can't lock yourself out
                user.is_active = data["is_active"]
            if data["password"]:
                user.set_password(data["password"])
            user.save()
            profile.role = data["role"]
            profile.phone = data["phone"]
            profile.save()
            log_activity(request.user, "user_updated", user,
                         note=f"role: {data['role'].name}"
                              + ("; password reset" if data["password"] else ""))
            messages.success(request, f"{user.username} saved."
                             + (" Password changed." if data["password"] else ""))
            return redirect("user_list")
    return render(request, "core/user_form.html", {"form": form, "edit_user": user})


# --- Roles -----------------------------------------------------------------
@admin_required
def role_list(request):
    return render(request, "core/role_list.html",
                  {"roles": Role.objects.prefetch_related("users")})


def _role_form(request, role=None):
    form = RoleForm(request.POST or None, instance=role)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.modules = [key for key, _, _ in MODULES
                       if request.POST.get(f"mod_{key}")]
        obj.save()
        log_activity(request.user, "role_saved", obj,
                     changes={"modules": obj.modules,
                              "can_see_costs": obj.can_see_costs})
        messages.success(request, f"Role “{obj.name}” saved.")
        return None
    return form


@admin_required
def role_create(request):
    form = _role_form(request)
    if form is None:
        return redirect("role_list")
    return render(request, "core/role_form.html",
                  {"form": form, "role": None, "modules": MODULES,
                   "checked": set()})


@admin_required
def role_edit(request, pk):
    role = get_object_or_404(Role, pk=pk)
    form = _role_form(request, role)
    if form is None:
        return redirect("role_list")
    return render(request, "core/role_form.html",
                  {"form": form, "role": role, "modules": MODULES,
                   "checked": set(role.modules)})


@admin_required
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if request.method == "POST":
        if role.is_system:
            messages.error(request, "Built-in roles can't be deleted (you can edit them).")
        elif role.users.exists():
            messages.error(request, "Reassign that role's users first.")
        else:
            log_activity(request.user, "role_deleted", role)
            role.delete()
            messages.success(request, "Role deleted.")
    return redirect("role_list")
