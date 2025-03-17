from django.shortcuts import render

from django.db import models


# Создаем представление (VIEW) для удобного доступа к основной информации о записях на прием
class AppointmentView(models.Model):
    """
    Модель представления (VIEW), объединяющая информацию о записи на прием
    из разных таблиц для удобства доступа и отображения.

    Это не обычная таблица, а VIEW в базе данных, которое будет создано
    с помощью миграции для прямого доступа ко всем необходимым данным.
    """
    # Поля из Appointment
    appointment_id = models.BigIntegerField(primary_key=True, verbose_name="ID записи")
    is_infoclinica_id = models.BooleanField(verbose_name="ID из Infoclinica")
    start_time = models.DateTimeField(verbose_name="Время начала приема")
    end_time = models.DateTimeField(null=True, verbose_name="Время окончания приема")
    cabinet_number = models.IntegerField(null=True, verbose_name="Номер кабинета")
    service_id = models.IntegerField(null=True, verbose_name="ID услуги")
    service_name = models.CharField(max_length=255, null=True, verbose_name="Название услуги")
    status = models.IntegerField(null=True, verbose_name="Статус записи")
    is_active = models.BooleanField(verbose_name="Активен")

    # Поля из Patient
    patient_code = models.BigIntegerField(verbose_name="Код пациента")
    patient_name = models.CharField(max_length=255, verbose_name="Имя пациента")
    patient_phone = models.CharField(max_length=20, null=True, verbose_name="Телефон пациента")

    # Поля из Doctor
    doctor_code = models.BigIntegerField(null=True, verbose_name="Код врача")
    doctor_name = models.CharField(max_length=255, null=True, verbose_name="Имя врача")
    doctor_specialization = models.CharField(max_length=255, null=True, verbose_name="Специализация")

    # Поля из Clinic
    clinic_id = models.IntegerField(null=True, verbose_name="ID клиники")
    clinic_name = models.CharField(max_length=255, null=True, verbose_name="Название клиники")

    # Поля из Department
    department_id = models.BigIntegerField(null=True, verbose_name="ID отделения")
    department_name = models.CharField(max_length=255, null=True, verbose_name="Название отделения")

    # Поля из QueueReason
    reason_id = models.IntegerField(null=True, verbose_name="ID причины")
    reason_name = models.CharField(max_length=255, null=True, verbose_name="Название причины")

    # Информация о звонках
    has_calls = models.BooleanField(verbose_name="Есть звонки")
    last_call_status = models.IntegerField(null=True, verbose_name="Статус последнего звонка")
    last_call_type = models.CharField(max_length=10, null=True, verbose_name="Тип последнего звонка")

    class Meta:
        managed = False  # Django не будет управлять этой таблицей
        db_table = 'appointment_view'  # Имя представления в БД
        verbose_name = "Просмотр записей на прием"
        verbose_name_plural = "Просмотр записей на прием"


# SQL для создания представления (добавить в миграцию)
"""
CREATE OR REPLACE VIEW appointment_view AS
SELECT 
    a.appointment_id,
    a.is_infoclinica_id,
    a.start_time,
    a.end_time,
    a.cabinet_number,
    a.service_id,
    a.service_name,
    a.status,
    a.is_active,

    p.patient_code,
    p.full_name AS patient_name,
    p.phone_mobile AS patient_phone,

    d.doctor_code,
    d.full_name AS doctor_name,
    d.specialization AS doctor_specialization,

    c.clinic_id,
    c.name AS clinic_name,

    dp.department_id,
    dp.name AS department_name,

    qr.reason_id,
    qr.reason_name,

    CASE WHEN COUNT(cl.id) > 0 THEN TRUE ELSE FALSE END AS has_calls,
    (SELECT status_id FROM reminder_call WHERE appointment_id = a.appointment_id ORDER BY created_at DESC LIMIT 1) AS last_call_status,
    (SELECT call_type FROM reminder_call WHERE appointment_id = a.appointment_id ORDER BY created_at DESC LIMIT 1) AS last_call_type

FROM 
    reminder_appointment a
LEFT JOIN 
    reminder_patient p ON a.patient_id = p.id
LEFT JOIN 
    reminder_doctor d ON a.doctor_id = d.id
LEFT JOIN 
    reminder_clinic c ON a.clinic_id = c.id
LEFT JOIN 
    reminder_department dp ON a.department_id = dp.id
LEFT JOIN 
    reminder_queuereason qr ON a.reason_id = qr.id
LEFT JOIN 
    reminder_call cl ON a.id = cl.appointment_id

GROUP BY 
    a.appointment_id,
    p.patient_code,
    d.doctor_code,
    c.clinic_id,
    dp.department_id,
    qr.reason_id;
"""
