from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
import uuid
from datetime import timedelta, datetime


def generate_uuid():
    return str(uuid.uuid4())


def calculate_expiry():
    return timezone.now() + timedelta(hours=24)


class Clinic(models.Model):
    """Клиника (филиал)"""
    clinic_id = models.IntegerField(unique=True, verbose_name="Идентификатор филиала (FILIAL)")
    name = models.CharField(max_length=255, verbose_name="Название филиала (FNAME)")
    address = models.TextField(blank=True, null=True, verbose_name="Адрес (FADDR)")
    phone = models.CharField(max_length=50, blank=True, null=True, verbose_name="Телефон (FPHONE)")
    email = models.EmailField(null=True, blank=True, verbose_name="Email (FMAIL)")
    timezone = models.IntegerField(default=3, verbose_name="Часовой пояс (TIMEZONE)")
    view_in_web = models.BooleanField(default=False, verbose_name="Отображать на сайте (VIEWINWEB_OUT)")
    work_hours = models.TextField(blank=True, null=True, verbose_name="Часы работы (WORKHOURS)")
    latitude = models.FloatField(null=True, blank=True, verbose_name="Широта (LATITUDE)")
    longitude = models.FloatField(null=True, blank=True, verbose_name="Долгота (LONGTITUDE)")

    class Meta:
        verbose_name = "Клиника"
        verbose_name_plural = "Клиники"

    def __str__(self):
        return f"Clinic {self.name} ({self.clinic_id})"


class Department(models.Model):
    """Отделение клиники"""
    department_id = models.BigIntegerField(unique=True, verbose_name="Идентификатор отделения (DEPNUM)")
    name = models.CharField(max_length=255, verbose_name="Название отделения (DEPNAME)")
    clinic = models.ForeignKey(
        Clinic, on_delete=models.CASCADE,
        related_name="departments",
        verbose_name="Филиал",
        null=True, blank=True
    )
    group_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Группа отделений (DEPGRPNAME)")
    view_in_web = models.BooleanField(default=False, verbose_name="Отображать на сайте (VIEWINWEB_OUT)")
    media_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="Идентификатор медиа (MEDIAID)")
    is_favorite = models.BooleanField(default=False, verbose_name="Избранное (ISFAVORITE)")
    comment = models.TextField(null=True, blank=True, verbose_name="Комментарий (COMMENT)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Отделение"
        verbose_name_plural = "Отделения"
        ordering = ["name"]

    def __str__(self):
        clinic_info = f" ({self.clinic.name})" if self.clinic else ""
        return f"{self.name}{clinic_info} [{self.department_id}]"


class Doctor(models.Model):
    """Врач"""
    doctor_code = models.BigIntegerField(unique=True, verbose_name="Идентификатор врача (DCODE)")
    full_name = models.CharField(max_length=255, verbose_name="ФИО врача (DNAME)")
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL,
        related_name="doctors",
        verbose_name="Отделение",
        null=True, blank=True
    )
    clinic = models.ForeignKey(
        Clinic, on_delete=models.SET_NULL,
        related_name="doctors",
        verbose_name="Основная клиника",
        null=True, blank=True
    )
    specialization = models.CharField(max_length=255, null=True, blank=True, verbose_name="Специализация")
    specialization_id = models.SmallIntegerField(null=True, blank=True, verbose_name="ID специализации")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Врач"
        verbose_name_plural = "Врачи"
        ordering = ["full_name"]

    def __str__(self):
        return f"Doctor {self.full_name} ({self.doctor_code})"


class Patient(models.Model):
    """Пациент"""
    GENDER_CHOICES = [(1, 'Мужской'), (2, 'Женский')]

    patient_code = models.BigIntegerField(unique=True, verbose_name="Идентификатор пациента (PCODE)")
    full_name = models.CharField(max_length=255, verbose_name="ФИО пациента (PNAME)")
    first_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="Имя")
    last_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="Фамилия")
    middle_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="Отчество")
    address = models.TextField(null=True, blank=True, verbose_name="Адрес пациента (PADDR)")
    phone_mobile = models.CharField(max_length=20, null=True, blank=True, verbose_name="Мобильный телефон (PPHONE)")
    phone_home = models.CharField(max_length=20, null=True, blank=True, verbose_name="Домашний телефон (HOMEPHONE)")
    phone_work = models.CharField(max_length=20, null=True, blank=True, verbose_name="Рабочий телефон (WORKPHONE)")
    email = models.EmailField(null=True, blank=True, verbose_name="Email (PMAIL)")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Дата рождения (BDATE)")
    gender = models.SmallIntegerField(null=True, blank=True, choices=GENDER_CHOICES,
                                      verbose_name="Пол пациента (GENDER)")
    refuse_email = models.BooleanField(null=True, blank=True, default=False,
                                       verbose_name="Отказ от почтовой рассылки (REFUSEMAIL)")
    refuse_call = models.BooleanField(null=True, blank=True, default=False,
                                      verbose_name="Отказ от обзвона (REFUSECALL)")
    refuse_sms = models.BooleanField(null=True, blank=True, default=False, verbose_name="Отказ от SMS (REFUSESMS)")
    refuse_messengers = models.BooleanField(null=True, blank=True, default=False,
                                            verbose_name="Отказ от мессенджеров (REFUSEMESSENGERS)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    last_used_doctor = models.ForeignKey(Doctor, on_delete=models.SET_NULL, null=True, blank=True)
    last_queue_reason_code = models.CharField(
        max_length=20, null=True, blank=True,
        verbose_name="Последняя причина очереди (код)"
    )
    last_queue_reason_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Последняя причина очереди (название)"
    )

    class Meta:
        verbose_name = "Пациент"
        verbose_name_plural = "Пациенты"

    def __str__(self):
        return f"Patient {self.patient_code} - {self.full_name}"

    def get_full_name(self):
        """Возвращает полное имя пациента"""
        if self.first_name and self.last_name:
            full_name = f"{self.last_name} {self.first_name}"
            if self.middle_name:
                full_name += f" {self.middle_name}"
            return full_name
        return self.full_name


class Appointment(models.Model):
    """Запись на прием (объединяет информацию из Reception и Patient)"""
    # Единый идентификатор для записи на прием
    appointment_id = models.BigIntegerField(primary_key=True, verbose_name="Идентификатор записи на прием")

    # Этот флаг указывает, был ли ID получен из Infoclinica (schedid) или из внутренней системы
    is_infoclinica_id = models.BooleanField(default=False, verbose_name="ID из Infoclinica")

    # Связи с другими моделями
    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE,
        related_name="appointments",
        verbose_name="Пациент"
    )
    doctor = models.ForeignKey(
        Doctor, on_delete=models.SET_NULL,
        related_name="appointments",
        verbose_name="Врач",
        null=True, blank=True
    )
    clinic = models.ForeignKey(
        Clinic, on_delete=models.SET_NULL,
        related_name="appointments",
        verbose_name="Клиника",
        null=True, blank=True
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL,
        related_name="appointments",
        verbose_name="Отделение",
        null=True, blank=True
    )
    # Добавляем прямую ссылку на причину, чтобы сократить количество JOINs
    reason = models.ForeignKey(
        'QueueReason', on_delete=models.SET_NULL,
        related_name="appointments",
        verbose_name="Причина записи",
        null=True, blank=True
    )

    # Свойства записи
    start_time = models.DateTimeField(verbose_name="Время начала приема")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="Время окончания приема")
    cabinet_number = models.IntegerField(null=True, blank=True, verbose_name="Номер кабинета")
    service_id = models.IntegerField(null=True, blank=True, verbose_name="ID услуги")
    service_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Название услуги")
    status = models.IntegerField(null=True, blank=True, verbose_name="Статус записи")

    # Свойства уведомлений (перенесено из Reception)
    processed_for_today = models.BooleanField(default=False, verbose_name="Обработан для сегодняшних уведомлений")
    processed_for_tomorrow = models.BooleanField(default=False, verbose_name="Обработан для завтрашних уведомлений")
    calltime_for_today = models.DateTimeField(null=True, blank=True,
                                              verbose_name="Время звонка для сегодняшних уведомлений")
    calltime_for_tomorrow = models.DateTimeField(null=True, blank=True,
                                                 verbose_name="Время звонка для завтрашних уведомлений")

    # Метаданные
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")
    is_active = models.BooleanField(default=True, verbose_name="Активен")

    class Meta:
        verbose_name = "Запись на прием"
        verbose_name_plural = "Записи на прием"
        ordering = ["-start_time"]

    def __str__(self):
        patient_name = self.patient.get_full_name() if self.patient else "Unknown"
        doctor_name = self.doctor.full_name if self.doctor else "Unknown Doctor"
        return f"Appointment {self.appointment_id} - {patient_name} - {doctor_name} at {self.start_time}"

    class Meta:
        verbose_name = "Запись на прием"
        verbose_name_plural = "Записи на прием"
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=['patient', 'start_time']),
            models.Index(fields=['doctor', 'start_time']),
            models.Index(fields=['clinic', 'start_time']),
            models.Index(fields=['is_active']),
        ]


class Call(models.Model):
    """Звонок пациенту"""
    CALL_TYPE_CHOICES = [
        ('today', 'Сегодня'),
        ('tomorrow', 'Завтра'),
        ('queue', 'Очередь')  # Добавим новый тип для звонков из очереди
    ]

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE,
        related_name="calls",
        verbose_name="Запись на прием",
        null=True, blank=True  # Делаем поле необязательным
    )
    patient_code = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Код пациента"
    )
    order_key = models.CharField(max_length=255, verbose_name="Ключ заказа в ACS")
    status_id = models.IntegerField(null=True, blank=True, verbose_name="Статус звонка")
    audio_link = models.TextField(null=True, blank=True, verbose_name="Ссылка на аудио")
    is_added = models.BooleanField(default=False, verbose_name="Добавлен")
    call_type = models.CharField(max_length=10, choices=CALL_TYPE_CHOICES, verbose_name="Тип звонка")
    queue_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID очереди")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Звонок"
        verbose_name_plural = "Звонки"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=['appointment', '-created_at']),
            models.Index(fields=['patient_code', '-created_at']),  # Индекс по коду пациента
            models.Index(fields=['order_key']),
            models.Index(fields=['call_type']),
            models.Index(fields=['queue_id']),  # Индекс по ID очереди
        ]


class QueueReason(models.Model):
    """Причина постановки в очередь"""
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
    Информация об очереди пациента.
    В новой схеме это вспомогательная таблица, которая содержит данные
    о состоянии обработки очереди и взаимодействии с системой Infoclinica.
    """
    queue_id = models.BigIntegerField(unique=True, verbose_name="Идентификатор очереди")
    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE,
        related_name="queue_entries",
        verbose_name="Пациент"
    )
    appointment = models.ForeignKey(
        Appointment, on_delete=models.SET_NULL,
        related_name="queue_entries",
        null=True, blank=True,
        verbose_name="Запись на прием"
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

    # Branch/Location info - для совместимости со старым кодом
    branch = models.ForeignKey(
        Clinic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_queues", verbose_name="Филиал звонка (FILIAL)",
        to_field='clinic_id'
    )
    target_branch = models.ForeignKey(
        Clinic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="target_queues", verbose_name="Филиал пациента (TOFILIAL)",
        to_field='clinic_id'
    )

    # Important: Reason for queue entry - это поле теперь дублируется в Appointment для прямого доступа
    reason = models.ForeignKey(
        QueueReason, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="queues", verbose_name="Причина постановки в очередь",
        to_field='reason_id'
    )

    # Doctor info - устаревшие поля, должны быть заменены на ссылку на Doctor в Appointment
    doctor_code = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор врача (DCODE)"
    )
    doctor_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="ФИО врача (DNAME)"
    )

    # Department info - устаревшие поля, должны быть заменены на ссылку на Department в Appointment
    department_number = models.BigIntegerField(
        null=True, blank=True,
        verbose_name="Идентификатор отделения (DEPNUM)"
    )
    department_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Название отделения (DEPNAME)"
    )

    internal_reason_code = models.CharField(
        max_length=20, null=True, blank=True,
        verbose_name="Внутренний код причины"
    )
    internal_reason_name = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="Внутреннее название причины"
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания записи")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления записи")

    class Meta:
        verbose_name = "Очередь"
        verbose_name_plural = "Очереди"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient']),
            models.Index(fields=['appointment']),
            models.Index(fields=['current_state']),
            models.Index(fields=['reason']),
        ]

    class Meta:
        verbose_name = "Очередь"
        verbose_name_plural = "Очереди"
        ordering = ['-created_at']

    def __str__(self):
        reason_str = f" - {self.reason.reason_name}" if self.reason else ""
        return f"Queue {self.queue_id} - {self.patient.full_name}{reason_str}"


class QueueContactInfo(models.Model):
    """Возможные действия для очереди пациента"""
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

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        verbose_name = "Возможное действие очереди"
        verbose_name_plural = "Возможные действия очередей"
        ordering = ['queue', 'next_state']

    def __str__(self):
        return f"Queue {self.queue.queue_id} → {self.next_state_name or 'Unknown State'}"


# Служебные модели (перенесены без изменений)
class ApiKey(models.Model):
    """API ключ для доступа к внешним системам"""
    key = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'API Key updated at {self.updated_at}'


class IgnoredPatient(models.Model):
    """Модель для пациентов, которых нужно игнорировать"""
    patient_code = models.BigIntegerField(unique=True)

    def __str__(self):
        return f"IgnoredPatient {self.patient_code}"


class CallStatus(models.Model):
    """Статус звонка"""
    status_id = models.IntegerField(unique=True, verbose_name="Идентификатор результата звонка")
    status_name = models.CharField(max_length=255, verbose_name="Наименование результата звонка")

    class Meta:
        verbose_name = "Статус звонка"
        verbose_name_plural = "Статусы звонков"
        ordering = ["status_id"]

    def __str__(self):
        return f"{self.status_id}: {self.status_name}"


class AppointmentStatus(models.Model):
    """Статус записи на прием"""
    status_id = models.IntegerField(unique=True, verbose_name="Идентификатор статуса записи")
    status_name = models.CharField(max_length=255, verbose_name="Описание статуса записи")

    class Meta:
        verbose_name = "Статус записи"
        verbose_name_plural = "Статусы записей"
        ordering = ["status_id"]

    def __str__(self):
        return f"{self.status_id}: {self.status_name}"

# ====================================================
#                 Assistant models
# ====================================================


class BatchTimeout(models.Model):
    batch_timeout_seconds = models.IntegerField()


class Thread(models.Model):
    thread_id = models.CharField(max_length=120, unique=False, default=generate_uuid, editable=False)
    order_key = models.CharField(max_length=120, unique=False, default=generate_uuid, editable=False)
    assistant = models.ForeignKey(
        'Assistant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='threads'
    )
    # Добавьте это поле
    appointment_id = models.BigIntegerField(null=True, blank=True)
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


class Assistant(models.Model):
    assistant_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    model = models.CharField(max_length=50, default="gpt-4-mini")
    instructions = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Assistant {self.assistant_id}\nName: {self.name}\nModel: {self.model}\nInstructions: {self.instructions}\nCreated at: {self.created_at}'


class AvailableTimeSlot(models.Model):
    """Доступные временные слоты для записи на прием"""
    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE,
        related_name="available_slots",
        verbose_name="Пациент"
    )
    date = models.DateField(verbose_name="Дата слота")
    time = models.TimeField(verbose_name="Время слота")
    doctor = models.ForeignKey(
        Doctor, on_delete=models.SET_NULL,
        related_name="available_slots",
        verbose_name="Врач",
        null=True, blank=True
    )
    clinic = models.ForeignKey(
        Clinic, on_delete=models.SET_NULL,
        related_name="available_slots",
        verbose_name="Клиника",
        null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Доступный временной слот"
        verbose_name_plural = "Доступные временные слоты"
        ordering = ["date", "time"]
        unique_together = ['patient', 'date', 'time']

    def __str__(self):
        return f"Слот {self.date} {self.time} для {self.patient.full_name}"


class QueueReasonMapping(models.Model):
    """Сопоставление причин постановки в очередь с внутренними кодами"""
    reason = models.ForeignKey(
        QueueReason, on_delete=models.CASCADE,
        related_name="mappings",
        verbose_name="Причина из Инфоклиники"
    )
    internal_code = models.CharField(
        max_length=20, verbose_name="Внутренний код"
    )
    internal_name = models.CharField(
        max_length=255, verbose_name="Внутреннее название"
    )

    class Meta:
        verbose_name = "Сопоставление причин"
        verbose_name_plural = "Сопоставления причин"
        unique_together = ['reason', 'internal_code']

    def __str__(self):
        return f"{self.reason.reason_name} → {self.internal_name} ({self.internal_code})"


class PatientDoctorAssociation(models.Model):
    """Ассоциация врача с пациентом"""
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='doctor_associations')
    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name='patient_associations')

    # Дополнительная информация
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_booking_date = models.DateTimeField(null=True, blank=True)
    is_preferred = models.BooleanField(default=False)  # Постоянно предпочитаемый врач
    booking_count = models.IntegerField(default=0)  # Количество записей к этому врачу

    class Meta:
        unique_together = ['patient', 'doctor']
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.patient.full_name} -> {self.doctor.full_name}"
