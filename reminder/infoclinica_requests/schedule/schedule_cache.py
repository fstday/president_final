from datetime import datetime, timedelta
from django.core.cache import cache
from django.utils import timezone
import logging
from django.core.cache import cache
from datetime import datetime

logger = logging.getLogger(__name__)

CACHE_DURATION = 60 * 15


def get_schedule_cache_key(patient_code):
    """
    Генерирует единый ключ для 7-дневного кэша расписания
    """
    # Получаем начало текущей недели для стабильности ключа
    today = datetime.now().date()
    return f"doct_schedule_{patient_code}_{today}"


def get_cached_schedule(patient_code):
    """
    Получает кэшированное расписание
    """
    try:
        cache_key = f"schedule_{patient_code}"
        cached_data = cache.get(cache_key)

        if cached_data:
            logger.info(f"Найдено кэшированное расписание для пациента {patient_code}")
            return cached_data

        logger.info(f"Кэшированное расписание для пациента {patient_code} не найдено")
        return None

    except Exception as e:
        logger.error(f"Ошибка при получении кэшированного расписания: {e}")
        return None


def cache_schedule(patient_code, schedule_data):
    """
    Кэширует расписание с улучшенной поддержкой нескольких врачей
    """
    try:
        cache_key = f"schedule_{patient_code}"

        # Определяем целевую клинику и структурируем данные по врачам
        target_clinic = None
        doctors_availability = {}

        if schedule_data.get('by_doctor'):
            by_doctor = schedule_data['by_doctor']

            for doctor_code, doctor_data in by_doctor.items():
                doctors_availability[doctor_code] = {
                    'name': doctor_data.get('doctor_name'),
                    'department_id': doctor_data.get('department_id'),
                    'department_name': doctor_data.get('department_name'),
                    'available_dates': []
                }

                for schedule in doctor_data.get('schedules', []):
                    if schedule.get('has_free_slots', False):
                        doctors_availability[doctor_code]['available_dates'].append({
                            'date': schedule.get('date_iso'),
                            'free_count': schedule.get('free_count', 0),
                            'clinic_id': schedule.get('clinic_id'),
                            'begin_time': schedule.get('begin_time'),
                            'end_time': schedule.get('end_time'),
                            'schedule_id': schedule.get('schedule_id')
                        })

                    if not target_clinic and schedule.get('clinic_id'):
                        target_clinic = schedule['clinic_id']
        else:
            # Обрабатываем старый формат с одним врачом
            schedules = schedule_data.get('schedules', [])
            if schedules:
                first_schedule = schedules[0]
                doctors_availability = {
                    first_schedule.get('doctor_code'): {
                        'name': first_schedule.get('doctor_name'),
                        'department_id': first_schedule.get('department_id'),
                        'department_name': first_schedule.get('department_name'),
                        'available_dates': []
                    }
                }

                for schedule in schedules:
                    if schedule.get('has_free_slots', False):
                        doctors_availability[schedule.get('doctor_code')]['available_dates'].append({
                            'date': schedule.get('date_iso'),
                            'free_count': schedule.get('free_count', 0),
                            'clinic_id': schedule.get('clinic_id'),
                            'begin_time': schedule.get('begin_time'),
                            'end_time': schedule.get('end_time'),
                            'schedule_id': schedule.get('schedule_id')
                        })

                    if not target_clinic and schedule.get('clinic_id'):
                        target_clinic = schedule['clinic_id']

        # Добавляем информацию о целевой клинике в кэш
        cache_data = {
            'timestamp': datetime.now().isoformat(),
            'schedules': schedule_data.get('schedules', []),
            'by_doctor': schedule_data.get('by_doctor', {}),
            'patient_code': patient_code,
            'success': schedule_data.get('success', True),
            'target_clinic': target_clinic,
            'doctors_availability': doctors_availability
        }

        # Устанавливаем кэш с 15-минутным истечением
        cache.set(cache_key, cache_data, timeout=900)

        logger.info(f"Расписание для пациента {patient_code} кэшировано на 15 минут")
        logger.info(f"Целевая клиника определена: {target_clinic}")
        logger.info(f"Найдено врачей с доступными датами: {len(doctors_availability)}")

        return True

    except Exception as e:
        logger.error(f"Ошибка при кэшировании расписания: {e}")
        return False


def check_day_has_slots_from_cache(patient_code, date_str, cached_data=None):
    """
    Проверяет наличие слотов для конкретного дня из кэшированных данных
    с поддержкой нескольких врачей
    """
    # Если кэш не передан, пытаемся получить его
    if not cached_data:
        cached_data = get_cached_schedule(patient_code)
        if not cached_data:
            return None

    data = cached_data.get('data', cached_data)

    # Проверяем, успешен ли результат
    if not data.get('success', False):
        return {'has_slots': False}

    # Преобразуем строку даты в объект datetime
    if date_str == "today":
        check_date = datetime.now().date()
    elif date_str == "tomorrow":
        check_date = (datetime.now() + timedelta(days=1)).date()
    else:
        check_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    # Проверяем доступность по всем врачам
    doctors_availability = data.get('doctors_availability', {})

    if doctors_availability:
        # Проверяем в новом формате с поддержкой нескольких врачей
        available_doctors = []

        for doctor_code, doctor_data in doctors_availability.items():
            for available_date in doctor_data.get('available_dates', []):
                if available_date.get('date') == date_str and available_date.get('free_count', 0) > 0:
                    available_doctors.append({
                        'doctor_code': doctor_code,
                        'doctor_name': doctor_data.get('name'),
                        'department_id': doctor_data.get('department_id'),
                        'clinic_id': available_date.get('clinic_id'),
                        'free_count': available_date.get('free_count')
                    })

        if available_doctors:
            # Выбираем врача с наибольшим количеством слотов
            best_doctor = max(available_doctors, key=lambda x: x['free_count'])

            return {
                'has_slots': True,
                'doctor_code': best_doctor['doctor_code'],
                'doctor_name': best_doctor['doctor_name'],
                'department_id': best_doctor['department_id'],
                'clinic_id': best_doctor['clinic_id'],
                'free_count': best_doctor['free_count']
            }
    else:
        # Обратная совместимость со старым форматом
        schedules = data.get('schedules', [])
        for slot in schedules:
            slot_date_str = slot.get('date_iso')
            if not slot_date_str:
                continue

            slot_date = datetime.strptime(slot_date_str, "%Y-%m-%d").date()
            if slot_date == check_date and slot.get('has_free_slots', False):
                return {
                    'has_slots': True,
                    'doctor_code': slot.get('doctor_code'),
                    'department_id': slot.get('department_id'),
                    'clinic_id': slot.get('clinic_id')
                }

    return {'has_slots': False}
