import os
import django
import logging
import json
from datetime import datetime, timedelta
from django.http import JsonResponse
from django.conf import settings

# Настройка Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.infoclinica_requests.schedule.doct_schedule_free import (
    get_patient_doctor_schedule, select_best_doctor_from_schedules, get_available_doctor_by_patient
)
from reminder.infoclinica_requests.schedule.schedule_cache import get_cached_schedule
from reminder.models import Patient, Doctor, PatientDoctorAssociation

logger = logging.getLogger(__name__)


def reserve_reception_for_patient(patient_id, date_from_patient, trigger_id=1):
    """
    Записывает пациента на прием к врачу с поддержкой автоматического выбора врача
    """
    try:
        logger.info(
            f"Запрос на запись/перенос: patient_id={patient_id}, date_from_patient={date_from_patient}, trigger_id={trigger_id}")

        # Получаем пациента
        patient = Patient.objects.filter(patient_code=patient_id).first()
        if not patient:
            return JsonResponse({
                "status": "error",
                "message": f"Пациент с кодом {patient_id} не найден"
            })

        # Парсим дату и время из запроса
        datetime_obj = None
        if " " in date_from_patient:
            try:
                datetime_obj = datetime.strptime(date_from_patient, "%Y-%m-%d %H:%M")
            except ValueError:
                logger.error(f"Неверный формат даты: {date_from_patient}")
                return JsonResponse({
                    "status": "error_change_reception_bad_date",
                    "message": "Неверный формат даты"
                })

        if not datetime_obj:
            return JsonResponse({
                "status": "error_change_reception_bad_date",
                "message": "Неверный формат даты"
            })

        requested_date = datetime_obj.strftime("%Y-%m-%d")
        requested_time = datetime_obj.strftime("%H:%M")

        # Получаем информацию о врачах
        doctor_code, department_id, clinic_id = get_available_doctor_by_patient(patient_id)

        # Если у нас нет конкретного врача, пытаемся выбрать подходящего
        if not doctor_code and department_id:
            # Получаем кэшированные данные или делаем новый запрос
            cached_result = get_cached_schedule(patient_id)

            if cached_result and 'by_doctor' in cached_result:
                by_doctor = cached_result['by_doctor']

                # Находим врачей с доступными слотами на запрашиваемое время
                available_doctors = []

                for doc_code, doctor_data in by_doctor.items():
                    for schedule in doctor_data.get('schedules', []):
                        if (schedule.get('date_iso') == requested_date and
                                schedule.get('begin_time') == requested_time and
                                schedule.get('has_free_slots', False)):
                            available_doctors.append({
                                'doctor_code': doc_code,
                                'doctor_name': doctor_data.get('doctor_name'),
                                'free_count': schedule.get('free_count', 1),
                                'department_id': doctor_data.get('department_id')
                            })

                # Выбираем врача с наибольшим количеством свободных слотов
                if available_doctors:
                    selected_doctor = max(available_doctors, key=lambda x: x['free_count'])
                    doctor_code = selected_doctor['doctor_code']

                    # Сохраняем эту ассоциацию врача для пациента
                    doctor_obj, _ = Doctor.objects.get_or_create(
                        doctor_code=doctor_code,
                        defaults={'full_name': selected_doctor['doctor_name']}
                    )

                    # Обновляем последнего использованного врача для пациента
                    patient.last_used_doctor = doctor_obj
                    patient.save()

                    logger.info(f"Автоматически выбран врач {doctor_code} для пациента {patient_id}")
                else:
                    # Используем первого доступного врача из отделения (последний запас)
                    if by_doctor:
                        first_available_doctor = next(iter(by_doctor.items()))
                        doctor_code = first_available_doctor[0]
                        doctor_name = first_available_doctor[1].get('doctor_name', '')

                        doctor_obj, _ = Doctor.objects.get_or_create(
                            doctor_code=doctor_code,
                            defaults={'full_name': doctor_name}
                        )

                        patient.last_used_doctor = doctor_obj
                        patient.save()

                        logger.info(f"Выбран запасной вариант врача {doctor_code} для пациента {patient_id}")

        # Если все еще нет врача, возвращаем ошибку
        if not doctor_code:
            return JsonResponse({
                "status": "error",
                "message": "Не удалось выбрать подходящего врача"
            })

        # Продолжаем с существующей логикой записи...
        # (Остальная часть кода функции остается без изменений)

    except Exception as e:
        logger.error(f"Ошибка в reserve_reception_for_patient: {e}", exc_info=True)
        return JsonResponse({
            "status": "error",
            "message": str(e)
        })
