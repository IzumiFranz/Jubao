from django.contrib import admin

from .models import UserPreference


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "personal_base_currency", "updated_at")
    list_select_related = ("user", "personal_base_currency")
    search_fields = ("user__username", "personal_base_currency__code")
    readonly_fields = ("created_at", "updated_at")
