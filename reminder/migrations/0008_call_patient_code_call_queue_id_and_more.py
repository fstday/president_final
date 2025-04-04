# Generated by Django 5.1.7 on 2025-04-03 11:08

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reminder', '0007_queueinfo_internal_reason_code_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='call',
            name='patient_code',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='Код пациента'),
        ),
        migrations.AddField(
            model_name='call',
            name='queue_id',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='ID очереди'),
        ),
        migrations.AddField(
            model_name='patient',
            name='last_queue_reason_code',
            field=models.CharField(blank=True, max_length=20, null=True, verbose_name='Последняя причина очереди (код)'),
        ),
        migrations.AddField(
            model_name='patient',
            name='last_queue_reason_name',
            field=models.CharField(blank=True, max_length=255, null=True, verbose_name='Последняя причина очереди (название)'),
        ),
        migrations.AlterField(
            model_name='call',
            name='appointment',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='calls', to='reminder.appointment', verbose_name='Запись на прием'),
        ),
        migrations.AlterField(
            model_name='call',
            name='call_type',
            field=models.CharField(choices=[('today', 'Сегодня'), ('tomorrow', 'Завтра'), ('queue', 'Очередь')], max_length=10, verbose_name='Тип звонка'),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(fields=['patient_code', '-created_at'], name='reminder_ca_patient_5f8b7c_idx'),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(fields=['queue_id'], name='reminder_ca_queue_i_c0a463_idx'),
        ),
    ]
