import os
import uuid
from datetime import timedelta, datetime

import django
from django.db import models
from django.utils import timezone
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _


def generate_uuid():
    return str(uuid.uuid4())


def calculate_expiry():
    return now() + timedelta(hours=24)

class BatchTimeout(models.Model):
    batch_timeout_seconds = models.IntegerField()


class Thread(models.Model):
    thread_id = models.CharField(max_length=120, unique=True, default=generate_uuid, editable=False)
    order_key = models.CharField(max_length=120, unique=True, default=generate_uuid, editable=False)
    assistant = models.ForeignKey(
        'Assistant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='threads'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=calculate_expiry)
    current_run = models.ForeignKey('Run',
                                    on_delete=models.SET_NULL,
                                    null=True,
                                    blank=True,
                                    related_name='active_thread')

    def __str__(self):
        return f'Thread {self.thread_id}'

    def is_expired(self):
        return timezone.now() > self.expires_at

    def can_add_message(self):
        """
        Check if messages can be added to the thread
        """
        if not self.current_run:
            return True
        return self.current_run.status == Run.Status.COMPLETED

class AigerimApiToken(models.Model):
    access_token = models.CharField(max_length=255)
    refresh_token = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Access Token: {self.access_token}, {self.created_at}"



class Run(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', _('Queued')
        IN_PROGRESS = 'in_progress', _('In Progress')
        REQUIRES_ACTION = 'requires_action', _('Requires Action')
        COMPLETED = 'completed', _('Completed')
        FAILED = 'failed', _('Failed')
        CANCELLED = 'cancelled', _('Cancelled')
        EXPIRED = 'expired', _('Expired')

    run_id = models.CharField(max_length=255, unique=True, verbose_name=_('OpenAI Run ID'))
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        verbose_name=_('Run Status')
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Creation Timestamp'))
    run_expired_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Run Expiration Timestamp'))

class Reception(models.Model):
    patient_code = models.BigIntegerField()
    phone_number = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    lastname = models.CharField(max_length=100)
    middlename = models.CharField(max_length=100, null=True, blank=True)
    reception_code = models.BigIntegerField(unique=True)
    start_time = models.DateTimeField()
    processed_for_today = models.BooleanField(default=False)
    processed_for_tomorrow = models.BooleanField(default=False)
    calltime_for_tomorrow = models.DateTimeField(null=True, blank=True)
    calltime_for_today = models.DateTimeField(null=True, blank=True)
    upload_time = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    #AIGERIM DATA
    specialist_code = models.BigIntegerField()
    specialization_id = models.SmallIntegerField()
    clinic_id = models.SmallIntegerField()
    service_id = models.SmallIntegerField()
    specialist_name = models.CharField(max_length=100)
    cabinet_number = models.IntegerField()

    def __str__(self):
        return f"Reception {self.reception_code} for patient {self.name} {self.lastname} ({self.patient_code}) created at {self.upload_time}"

class Call(models.Model):
    reception = models.ForeignKey(Reception, on_delete=models.CASCADE, related_name="calls")
    order_key = models.CharField(max_length=255)
    status_id = models.IntegerField(null=True, blank=True)
    audio_link = models.TextField(null=True, blank=True)
    is_added = models.BooleanField(default=False)
    call_type = models.CharField(max_length=10, choices=[('today', 'Сегодня'), ('tomorrow', 'Завтра')])

    def __str__(self):
        return f"Call for reception {self.reception.reception_code} with order_key {self.order_key}"


class ApiKey(models.Model):
    key = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'API Key updated at {self.updated_at}'


class IgnoredPatient(models.Model):
    patient_code = models.BigIntegerField(unique=True)

    def __str__(self):
        return f"IgnoredPatient {self.patient_code}"


class Assistant(models.Model):
    assistant_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    model = models.CharField(max_length=50, default="gpt-4-mini")
    instructions = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Assistant {self.assistant_id}\nName: {self.name}\nModel: {self.model}\nInstructions: {self.instructions}\nCreated at: {self.created_at}'


class CallStatus(models.Model):
    status_id = models.IntegerField(unique=True, verbose_name="Идентификатор результата звонка")
    status_name = models.CharField(max_length=255, verbose_name="Наименование результата звонка")

    class Meta:
        verbose_name = "Статус звонка"
        verbose_name_plural = "Статусы звонков"
        ordering = ["status_id"]

    def __str__(self):
        return f"{self.status_id}: {self.status_name}"


class AppointmentStatus(models.Model):
    status_id = models.IntegerField(unique=True, verbose_name="Идентификатор статуса записи")
    status_name = models.CharField(max_length=255, verbose_name="Описание статуса записи")

    class Meta:
        verbose_name = "Статус записи"
        verbose_name_plural = "Статусы записей"
        ordering = ["status_id"]

    def __str__(self):
        return f"{self.status_id}: {self.status_name}"


class Patient(models.Model):
    GENDER_CHOICES = [(1, 'Мужской'), (2, 'Женский')]

    patient_code = models.BigIntegerField(unique=True, verbose_name="Идентификатор пациента (PCODE)")
    full_name = models.CharField(max_length=255, verbose_name="ФИО пациента (PNAME)")
    schedid = models.BigIntegerField(null=True, blank=True, verbose_name="Идентификатор записи (SCHEDID)")
    address = models.TextField(null=True, blank=True, verbose_name="Адрес пациента (PADDR)")
    phone_mobile = models.CharField(max_length=20, null=True, blank=True, verbose_name="Мобильный телефон (PPHONE)")
    phone_home = models.CharField(max_length=20, null=True, blank=True, verbose_name="Домашний телефон (HOMEPHONE)")
    phone_work = models.CharField(max_length=20, null=True, blank=True, verbose_name="Рабочий телефон (WORKPHONE)")
    email = models.EmailField(null=True, blank=True, verbose_name="Email (PMAIL)")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Дата рождения (BDATE)")
    gender = models.SmallIntegerField(null=True, blank=True, choices=GENDER_CHOICES, verbose_name="Пол пациента (GENDER)")
    refuse_email = models.BooleanField(null=True, blank=True, default=False, verbose_name="Отказ от почтовой рассылки (REFUSEMAIL)")
    refuse_call = models.BooleanField(null=True, blank=True, default=False, verbose_name="Отказ от обзвона (REFUSECALL)")
    refuse_sms = models.BooleanField(null=True, blank=True, default=False, verbose_name="Отказ от SMS (REFUSESMS)")
    refuse_messengers = models.BooleanField(null=True, blank=True, default=False, verbose_name="Отказ от мессенджеров (REFUSEMESSENGERS)")

    class Meta:
        verbose_name = "Пациент"
        verbose_name_plural = "Пациенты"

    def __str__(self):
        return f"Patient {self.patient_code} - {self.full_name}"


class QueueReason(models.Model):
    """
    Хранит причины постановки в очередь (ADDID/ADDNAME)
    """
    reason_id = models.IntegerField(unique=True, verbose_name="Идентификатор причины (ADDID)")
    reason_name = models.CharField(max_length=255, verbose_name="Название причины (ADDNAME)")

    class Meta:
        verbose_name = "Причина очереди"
        verbose_name_plural = "Причины очередей"
        ordering = ["reason_id"]

    def __str__(self):
        return f"{self.reason_id}: {self.reason_name}"


class QueueInfo(models.Model):
    """
    Основная информация об очереди пациента
    """
    queue_id = models.BigIntegerField(unique=True, verbose_name="Идентификатор очереди")
    patient = models.ForeignKey(
        'Patient', on_delete=models.CASCADE, related_name="queue_entries",
        verbose_name="Пациент"
    )

    # Current state info
    current_state = models.IntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор текущего состояния контакта (CURRENTSTATE)"
    )
    current_state_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Наименование текущего состояния (CURRENTSTATENAME)"
    )

    # Default next state info
    default_next_state = models.IntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор следующего состояния (DEFAULTNEXTSTATE)"
    )
    default_next_state_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Наименование следующего состояния (DEFAULTNEXTSTATENAME)"
    )

    # Date ranges
    contact_start_date = models.DateField(
        null=True, blank=True,
        verbose_name="Дата начала контакта (CONTACTBDATE)"
    )
    contact_end_date = models.DateField(
        null=True, blank=True,
        verbose_name="Дата окончания контакта (CONTACTFDATE)"
    )
    desired_start_date = models.DateField(
        null=True, blank=True,
        verbose_name="Дата начала желаемой записи пациента (ACTIONBDATE)"
    )
    desired_end_date = models.DateField(
        null=True, blank=True,
        verbose_name="Дата окончания желаемой записи пациента (ACTIONFDATE)"
    )

    # Branch/Location info
    branch = models.ForeignKey(
        'Clinic', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_queues", verbose_name="Филиал звонка (FILIAL)",
        to_field='clinic_id'
    )
    clinic_id_msh_99 = models.ForeignKey(
        'Clinic', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="target_queues", verbose_name="Филиал пациента (TOFILIAL)",
        to_field='clinic_id'
    )

    # Important: Reason for queue entry
    reason = models.ForeignKey(
        QueueReason, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="queues", verbose_name="Причина постановки в очередь",
        to_field='reason_id'
    )

    # Doctor info
    doctor_code = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор врача (DCODE)"
    )
    doctor_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="ФИО врача (DNAME)"
    )

    # Status
    appointment_status = models.ForeignKey(
        'AppointmentStatus', on_delete=models.SET_NULL,
        null=True, blank=True, related_name="appointments",
        verbose_name="Статус записи пациента"
    )

    # Additional fields for department
    department_number = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор отделения (DEPNUM)"
    )
    department_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Название отделения (DEPNAME)"
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Очередь"
        verbose_name_plural = "Очереди"
        ordering = ['-created_at']

    def __str__(self):
        reason_str = f" - {self.reason.reason_name}" if self.reason else ""
        return f"Queue {self.queue_id} - {self.patient.full_name}{reason_str}"


class CallResult(models.Model):
    queue = models.ForeignKey(QueueInfo, on_delete=models.CASCADE, related_name="call_results", verbose_name="Очередь")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="call_results", verbose_name="Пациент")
    call_date_time = models.DateTimeField(verbose_name="Дата и время звонка (CALLDATETIME)")
    next_call_date_time = models.DateTimeField(null=True, blank=True, verbose_name="Дата и время следующего звонка (NEXTCALLDATETIME)")
    next_operator_code = models.BigIntegerField(null=True, blank=True, verbose_name="Идентификатор сотрудника следующего звонка (NEXTDCODE)")
    next_operator_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="ФИО сотрудника следующего звонка (NEXTDNAME)")
    call_status = models.ForeignKey(CallStatus, on_delete=models.SET_NULL, null=True, blank=True, related_name="call_statuses", verbose_name="Статус звонка")
    call_comment = models.TextField(null=True, blank=True, verbose_name="Комментарий к звонку (CALLCOMMENT)")

    class Meta:
        verbose_name = "Результат звонка"
        verbose_name_plural = "Результаты звонков"

    def __str__(self):
        return f"Call result for {self.patient.full_name}"


class Clinic(models.Model):
    clinic_id = models.IntegerField(unique=True, verbose_name="Идентификатор филиала (FILIAL)")
    name = models.CharField(max_length=255, verbose_name="Название филиала (FNAME)")

    # Необязательные поля
    address = models.TextField(blank=True, null=True, verbose_name="Адрес (FADDR)")
    phone = models.CharField(max_length=50, blank=True, null=True, verbose_name="Телефон (FPHONE)")
    email = models.EmailField(null=True, blank=True, verbose_name="Email (FMAIL)")
    timezone = models.IntegerField(default=3, verbose_name="Часовой пояс (TIMEZONE)")

    # Дополнительные поля, которые могут быть полезны
    view_in_web = models.BooleanField(default=False, verbose_name="Отображать на сайте (VIEWINWEB_OUT)")
    work_hours = models.TextField(blank=True, null=True, verbose_name="Часы работы (WORKHOURS)")
    latitude = models.FloatField(null=True, blank=True, verbose_name="Широта (LATITUDE)")
    longitude = models.FloatField(null=True, blank=True, verbose_name="Долгота (LONGTITUDE)")

    class Meta:
        verbose_name = "Клиника"
        verbose_name_plural = "Клиники"

    def __str__(self):
        return f"Clinic {self.name} ({self.clinic_id})"


class QueueContactInfo(models.Model):
    """
    Хранит возможные действия для очереди пациента
    """
    queue = models.ForeignKey(
        QueueInfo, on_delete=models.CASCADE, related_name="contacts",
        verbose_name="Родительская очередь"
    )
    parent_action_id = models.IntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор родительского контакта (PARENTACTIONID)"
    )

    # Doctor/Operator info
    next_dcode = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор сотрудника следующего звонка (NEXTDCODE)"
    )
    next_dname = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="ФИО сотрудника следующего звонка (NEXTDNAME)"
    )

    # Next state info - IMPORTANT!
    next_state = models.IntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор следующего состояния (NEXTSTATE)"
    )
    next_state_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Наименование следующего состояния (NEXTSTATENAME)"
    )

    # Scheduling
    next_call_datetime = models.DateTimeField(
        null=True, blank=True,
        verbose_name="Дата и время следующего звонка (NEXTCALLDATETIME)"
    )

    # Metadata - Fixed by using only one of auto_now_add or default
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания"
    )

    class Meta:
        verbose_name = "Возможное действие очереди"
        verbose_name_plural = "Возможные действия очередей"
        ordering = ['queue', 'next_state']

    def __str__(self):
        return f"Queue {self.queue.queue_id} → {self.next_state_name or 'Unknown State'}"


class Department(models.Model):
    """
    Информация об отделениях клиник
    """
    department_id = models.BigIntegerField(unique=True, verbose_name="Идентификатор отделения (DEPNUM)")
    name = models.CharField(max_length=255, verbose_name="Название отделения (DEPNAME)")

    # Связь с клиникой (филиалом)
    clinic = models.ForeignKey(
        'Clinic', on_delete=models.CASCADE,
        related_name="departments",
        verbose_name="Филиал",
        null=True, blank=True
    )

    # Дополнительные поля
    group_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Группа отделений (DEPGRPNAME)")
    view_in_web = models.BooleanField(default=False, verbose_name="Отображать на сайте (VIEWINWEB_OUT)")
    media_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="Идентификатор медиа (MEDIAID)")
    is_favorite = models.BooleanField(default=False, verbose_name="Избранное (ISFAVORITE)")
    comment = models.TextField(null=True, blank=True, verbose_name="Комментарий (COMMENT)")

    # Метаданные
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Отделение"
        verbose_name_plural = "Отделения"
        ordering = ["name"]

    def __str__(self):
        clinic_info = f" ({self.clinic.name})" if self.clinic else ""
        return f"{self.name}{clinic_info} [{self.department_id}]"
