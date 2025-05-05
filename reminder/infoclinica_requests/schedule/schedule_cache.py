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
    Кэширует расписание и определяет целевую клиникую
    """
    try:
        cache_key = f"schedule_{patient_code}"

        # Определяем целевую клинику из расписания
        target_clinic = None
        if schedule_data.get('schedules'):
            for schedule in schedule_data['schedules']:
                if schedule.get('clinic_id'):
                    target_clinic = schedule['clinic_id']
                    break

        # Добавляем информацию о целевой клинике в кэш
        cache_data = {
            'timestamp': datetime.now().isoformat(),
            'schedules': schedule_data.get('schedules', []),
            'by_doctor': schedule_data.get('by_doctor', {}),
            'patient_code': patient_code,
            'success': schedule_data.get('success', True),
            'target_clinic': target_clinic
        }

        # Устанавливаем кэш с 15-минутным истечением
        cache.set(cache_key, cache_data, timeout=900)

        logger.info(f"Расписание для пациента {patient_code} кэшировано на 15 минут")
        logger.info(f"Целевая клиника определена: {target_clinic}")

    except Exception as e:
        logger.error(f"Ошибка при кэшировании расписания: {e}")


def check_day_has_slots_from_cache(patient_code, date_str, cached_data=None):
    """
    Проверяет наличие слотов для конкретного дня из кэшированных данных
    """
    # Если кэш не передан, пытаемся получить его
    if not cached_data:
        cached_data = get_cached_schedule(patient_code)
        if not cached_data:
            return None

    data = cached_data['data']

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

    # Ищем слоты на эту дату среди всех полученных дней в кэше
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
