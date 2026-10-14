from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from core.models import MODULE_KEYS, Role, UserProfile
from core.roles import (ALL_GROUPS, FULFILLMENT, FULFILLMENT_MODULES,
                        OFFICE_MODULES, WAREHOUSE_MODULES)

SYSTEM_ROLES = [
    # (name, modules, can_see_costs, is_admin_role)
    ("Admin", MODULE_KEYS, True, True),
    ("Office", OFFICE_MODULES, False, False),
    ("Warehouse", WAREHOUSE_MODULES, False, False),
    (FULFILLMENT, FULFILLMENT_MODULES, False, False),
]


class Command(BaseCommand):
    help = ("Create the SFL ERP System system roles + legacy groups, and attach "
            "role profiles to existing users.")

    def handle(self, *args, **options):
        for name in ALL_GROUPS:
            Group.objects.get_or_create(name=name)

        roles = {}
        for name, modules, costs, is_admin_role in SYSTEM_ROLES:
            role, created = Role.objects.get_or_create(
                name=name,
                defaults={"modules": list(modules), "can_see_costs": costs,
                          "is_admin_role": is_admin_role, "is_system": True})
            if not role.is_system:
                role.is_system = True
                role.save(update_fields=["is_system"])
            roles[name] = role
            self.stdout.write(f"{'created' if created else 'exists '}: role {name}")

        # Attach profiles to users that don't have one, mapped from legacy groups
        for user in User.objects.all():
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if profile.role_id:
                continue
            if user.is_superuser or user.groups.filter(name="Admin").exists():
                profile.role = roles["Admin"]
            elif user.groups.filter(name="Office").exists():
                profile.role = roles["Office"]
            elif user.groups.filter(name="Warehouse").exists():
                profile.role = roles["Warehouse"]
            if profile.role_id:
                profile.save(update_fields=["role"])
                self.stdout.write(f"profile: {user.username} -> {profile.role.name}")
