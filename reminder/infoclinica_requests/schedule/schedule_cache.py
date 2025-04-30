from datetime import datetime, timedelta
from django.core.cache import cache
from django.utils import timezone

# Кэш на 15 минут (в секундах)
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
    Получает закэшированное 7-дневное расписание или None
    """
    cache_key = get_schedule_cache_key(patient_code)
    return cache.get(cache_key)


def cache_schedule(patient_code, schedule_data):
    """
    Сохраняет 7-дневное расписание в кэш на 15 минут
    """
    cache_key = get_schedule_cache_key(patient_code)

    # Сохраняем с датой создания для отслеживания актуальности
    data_to_cache = {
        'created_at': timezone.now().isoformat(),
        'data': schedule_data
    }

    cache.set(cache_key, data_to_cache, CACHE_DURATION)
    return True


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
