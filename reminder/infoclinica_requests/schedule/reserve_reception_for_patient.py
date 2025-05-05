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
from reminder.models import Patient, Doctor, PatientDoctorAssociation, Appointment

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

            if cached_result and 'by_doctor' in cached_result.get('data', cached_result):
                by_doctor = cached_result.get('data', cached_result)['by_doctor']

                # Находим врачей с доступными слотами на запрашиваемую дату
                available_doctors = []

                for doc_code, doctor_data in by_doctor.items():
                    for schedule in doctor_data.get('schedules', []):
                        if (schedule.get('date_iso') == requested_date and
                                schedule.get('has_free_slots', False)):
                            available_doctors.append({
                                'doctor_code': doc_code,
                                'doctor_name': doctor_data.get('doctor_name'),
                                'free_count': schedule.get('free_count', 1),
                                'department_id': doctor_data.get('department_id'),
                                'schedules': doctor_data.get('schedules', [])
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

                    # Сохраняем ассоциацию врача с пациентом
                    PatientDoctorAssociation.objects.update_or_create(
                        patient=patient,
                        defaults={'doctor': doctor_obj}
                    )

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

                        # Сохраняем ассоциацию врача с пациентом
                        PatientDoctorAssociation.objects.update_or_create(
                            patient=patient,
                            defaults={'doctor': doctor_obj}
                        )

                        logger.info(f"Выбран запасной вариант врача {doctor_code} для пациента {patient_id}")

        # Если все еще нет врача, возвращаем ошибку
        if not doctor_code:
            return JsonResponse({
                "status": "error",
                "message": "Не удалось выбрать подходящего врача"
            })

        # Import необходимых модулей для резервирования
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
        from reminder.infoclinica_requests.schedule.schedule_rec_reserve import schedule_rec_reserve

        # Определяем TOFILIAL
        target_branch_id = clinic_id or 1

        # Проверяем, является ли это переносом существующей записи
        try:
            appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time').first()
            is_reschedule = bool(appointment)
            existing_schedid = appointment.appointment_id if appointment else None
        except Exception as e:
            logger.error(f"Ошибка при проверке существующих записей: {e}")
            is_reschedule = False
            existing_schedid = None

        # Получаем доступные времена для указанной даты
        times_result = which_time_in_certain_day(patient_id, requested_date)

        # Обрабатываем результат, если это JsonResponse
        if hasattr(times_result, 'content'):
            times_data = json.loads(times_result.content.decode('utf-8'))
        else:
            times_data = times_result

        # Проверяем наличие свободных времен
        if times_data.get('status', '').startswith('error_empty_windows'):
            # Если trigger_id == 3, просто возвращаем информацию об отсутствии слотов
            if trigger_id == 3:
                return JsonResponse({
                    "status": "error_empty_windows",
                    "message": "Нет свободных слотов на указанную дату"
                })
            else:
                return JsonResponse(times_data)

        # Извлекаем доступные времена
        available_times = []
        if 'all_available_times' in times_data:
            available_times = times_data['all_available_times']
        else:
            for key in ['first_time', 'second_time', 'third_time']:
                if key in times_data and times_data[key]:
                    available_times.append(times_data[key])

        # Если trigger_id == 3, просто возвращаем подтверждение о наличии слотов
        if trigger_id == 3:
            return JsonResponse({
                "status": "success_check_availability",
                "message": "Слоты доступны",
                "patient_id": patient_id,
                "date": requested_date,
                "time": requested_time,
                "doctor_code": doctor_code,
                "available_times": available_times
            })

        # Обрабатываем trigger_id == 2 (поиск альтернативного времени)
        if trigger_id == 2:
            from reminder.infoclinica_requests.utils import compare_and_suggest_times

            # Конвертируем requested_time в time object
            hour, minute = map(int, requested_time.split(':'))
            requested_time_obj = datetime.strptime(requested_time, "%H:%M").time()

            # Создаем список свободных интервалов из доступных времен
            free_intervals = []
            for t in available_times:
                start_hour, start_min = map(int, t.split(':'))
                # Предполагаем 30-минутные интервалы
                end_hour = start_hour
                end_min = start_min + 30
                if end_min >= 60:
                    end_min -= 60
                    end_hour += 1

                free_intervals.append({
                    'start_time': t,
                    'end_time': f"{end_hour:02d}:{end_min:02d}"
                })

            # Ищем ближайшие альтернативы
            suggested_times = compare_and_suggest_times(free_intervals, requested_time_obj, requested_date)

            # Форматируем ответ для системы
            result = {
                "status": "suggest_times",
                "suggested_times": suggested_times,
                "specialist_name": times_data.get('specialist_name', 'Специалист'),
                "patient_id": patient_id,
                "date": requested_date,
                "doctor_code": doctor_code
            }

            return JsonResponse(result)

        # Для trigger_id == 1, проверяем, доступно ли запрашиваемое время
        exact_time_available = requested_time in available_times

        if not exact_time_available:
            # Точное время недоступно, нужно вернуть альтернативы
            from reminder.infoclinica_requests.utils import compare_and_suggest_times

            # Создаем список свободных интервалов
            free_intervals = []
            for t in available_times:
                start_hour, start_min = map(int, t.split(':'))
                end_hour = start_hour
                end_min = start_min + 30
                if end_min >= 60:
                    end_min -= 60
                    end_hour += 1

                free_intervals.append({
                    'start_time': t,
                    'end_time': f"{end_hour:02d}:{end_min:02d}"
                })

            # Получаем предложенные времена
            hour, minute = map(int, requested_time.split(':'))
            requested_time_obj = datetime.strptime(requested_time, "%H:%M").time()
            suggested_times = compare_and_suggest_times(free_intervals, requested_time_obj, requested_date)

            # Возвращаем предложение с альтернативами
            result = {
                "status": "suggest_times",
                "suggested_times": suggested_times,
                "specialist_name": times_data.get('specialist_name', 'Специалист'),
                "patient_id": patient_id,
                "date": requested_date,
                "doctor_code": doctor_code
            }

            return JsonResponse(result)

        # Если точное время доступно, выполняем резервирование
        # Сначала нужно получить schedident для этого врача и даты
        from reminder.infoclinica_requests.schedule.schedule_cache import check_day_has_slots_from_cache

        # Получаем schedident из кэша для выбранного врача
        schedident = None
        if cached_result and 'by_doctor' in cached_result.get('data', cached_result):
            by_doctor = cached_result.get('data', cached_result)['by_doctor']
            if doctor_code in by_doctor:
                for schedule in by_doctor[doctor_code].get('schedules', []):
                    if schedule.get('date_iso') == requested_date:
                        schedident = schedule.get('schedule_id')
                        break

        if not schedident:
            return JsonResponse({
                "status": "error",
                "message": "Не удалось получить идентификатор расписания"
            })

        # Создаем list свободных интервалов для резервирования
        free_intervals = []
        for t in available_times:
            start_hour, start_min = map(int, t.split(':'))
            end_hour = start_hour
            end_min = start_min + 30
            if end_min >= 60:
                end_min -= 60
                end_hour += 1

            free_intervals.append({
                'start_time': t,
                'end_time': f"{end_hour:02d}:{end_min:02d}"
            })

        # Вызываем функцию резервирования
        reserve_result = schedule_rec_reserve(
            result_time=datetime_obj,
            doctor_id=doctor_code,
            date_part=requested_date,
            patient_id=patient_id,
            date_obj=datetime_obj,
            schedident_text=schedident,
            free_intervals=free_intervals,
            is_reschedule=is_reschedule,
            schedid=existing_schedid
        )

        # После успешного бронирования сохраняем ассоциацию врача
        if reserve_result.get("status") in ["success_schedule", "success_change_reception"] or \
                reserve_result.get("status", "").startswith("success_change_reception"):

            # Сохраняем ассоциацию врача с пациентом
            if doctor_code:
                try:
                    doctor_obj, created = Doctor.objects.get_or_create(
                        doctor_code=doctor_code,
                        defaults={'full_name': reserve_result.get("specialist_name", "")}
                    )

                    # Обновляем имя врача, если оно есть в ответе
                    if not created and reserve_result.get("specialist_name"):
                        doctor_obj.full_name = reserve_result.get("specialist_name")
                        doctor_obj.save()

                    # Обновляем последнего использованного врача для пациента
                    patient.last_used_doctor = doctor_obj
                    patient.save()

                    # Сохраняем ассоциацию врача с пациентом
                    association, _ = PatientDoctorAssociation.objects.update_or_create(
                        patient=patient,
                        defaults={'doctor': doctor_obj}
                    )

                    # Обновляем статистику ассоциации
                    association.last_booking_date = datetime.now()
                    association.booking_count += 1
                    association.save()

                    logger.info(f"Ассоциирован врач {doctor_code} с пациентом после успешной записи")
                except Exception as e:
                    logger.error(f"Ошибка при сохранении ассоциации врача после записи: {e}")

        return reserve_result

    except Exception as e:
        logger.error(f"Ошибка в reserve_reception_for_patient: {e}", exc_info=True)
        return JsonResponse({
            "status": "error",
            "message": str(e)
        })
