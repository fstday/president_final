from django.contrib import admin
from .models import QueueReasonMapping


@admin.register(QueueReasonMapping)
class QueueReasonMappingAdmin(admin.ModelAdmin):
    list_display = ('reason', 'internal_code', 'internal_name')
    search_fields = ('reason__reason_name', 'internal_code', 'internal_name')
    list_filter = ('internal_code',)
