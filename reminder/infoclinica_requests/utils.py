import os
import django
import requests
import json
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

import logging
logger = logging.getLogger(__name__)
import logging
from datetime import timedelta
import os
from multiprocessing.connection import answer_challenge


from reminder.models import Patient, Appointment, Doctor
import requests
import xml.etree.ElementTree as ET
import json
import time
import django
import pytz
import redis
import uuid
import base64
import re
from django.db.models.expressions import result
from django.http import JsonResponse

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from django.utils.timezone import make_aware
from reminder.models import Appointment, Call, ApiKey, IgnoredPatient
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from datetime import datetime, timedelta, time
from dotenv import load_dotenv


load_dotenv()

cert_pem = os.getenv('CERT_PEM')
key_pem = os.getenv('KEY_PEM')

# Определение пути к директории проекта
base_dir = os.path.dirname(os.path.abspath(__file__))

# Определение пути к директории certs, относительно базовой директории
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)

# Определение пути к сертификатам
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')

current_date_time_for_xml = datetime.now().strftime('%Y%m%d%H%M%S')

# Соединение с Redis
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


def normalize_time_for_receptions(free_intervals):
    """
    Функция для перевода времени из ответа сервера к формату HH:MM

    :param free_intervals:
    :return:
    """

    return datetime.strptime(free_intervals, '%H:%M').time()


def normalize_time_for_compare(time_str):
    """Пример нормализации строки времени в объект time."""
    return time.fromisoformat(time_str)  # Предполагаем, что строка в формате 'HH:MM'


def normalize_time_for_returning_answer(start_time):
    """
    Нормализуем строку времени в объект time.

    :param start_time: Строка времени в формате 'H:M'
    :return: Объект time
    """
    hour, minute = map(int, start_time.split(':'))
    return time(hour, minute)


def format_time_with_date(date_str, time_obj):
    """
    Форматируем строку времени с датой.

    :param date_str: Дата в формате 'YYYY-MM-DD'
    :param time_obj: Объект time
    :return: Строка в формате 'YYYY-MM-DD HH:MM'
    """
    return f"{date_str} {time_obj.strftime('%H:%M')}"


def compare_times(free_intervals, user_time, user_date):
    """
    Сравниваем время начала записи предпочитаемым пациенту, чтобы перезаписаться, и свободное время записей, пришедших из
    ответа сервера. Возвращает точное совпадение или все свободные времена с датой.

    Улучшенная версия, которая также находит ближайшее доступное время к запрошенному.

    Args:
        free_intervals: Список свободных интервалов времени
        user_time: Время пользователя в формате 'HH:MM:SS' или объект time
        user_date: Дата в формате 'YYYY-MM-DD'

    Returns:
        str or list: Время пользователя, если есть совпадение, или все свободные времена с датой
    """

    logger.info(f'Нахожусь в функции compare_times\nЖелаемое время пользователя: {user_time}\n')

    # Проверяем тип user_time и преобразуем его, если это необходимо
    if isinstance(user_time, str):
        user_time_obj = datetime.strptime(user_time, '%H:%M:%S').time()
    elif isinstance(user_time, time):
        user_time_obj = user_time
    else:
        raise ValueError("user_time должен быть строкой или объектом time")

    # Проверяем совпадение времени пользователя с доступными интервалами
    for interval in free_intervals:
        start_time = normalize_time_for_returning_answer(interval['start_time'])

        # Пропускаем интервалы меньше 09:00 и больше или равно 21:00
        if start_time < time(9, 0) or start_time >= time(21, 0):
            continue

        if start_time == user_time_obj:
            logger.info(f"Время пользователя совпадает с интервалом {interval['start_time']} - {interval['end_time']}")
            return f"{user_date} {user_time_obj.strftime('%H:%M')}"

    # Если совпадений нет, возвращаем все доступные времена
    available_times = []

    for interval in free_intervals:
        start_time = normalize_time_for_returning_answer(interval['start_time'])

        # Пропускаем интервалы меньше 09:00 и больше или равно 21:00
        if start_time < time(9, 0) or start_time >= time(21, 0):
            continue

        available_times.append(format_time_with_date(user_date, start_time))

    # Сортируем доступные времена
    available_times.sort()

    logger.info(f"Нет совпадений. Возвращаем все доступные времена: {len(available_times)} слотов")
    return available_times


def find_nearest_available_time(available_times, requested_time):
    """
    Находит ближайшее доступное время к запрошенному времени.

    Args:
        available_times: Список доступных времен в формате 'YYYY-MM-DD HH:MM'
        requested_time: Запрошенное время в формате 'HH:MM' или объект time

    Returns:
        str: Ближайшее доступное время в формате 'YYYY-MM-DD HH:MM'
    """
    if not available_times:
        return None

    # Преобразуем запрошенное время в минуты от начала дня
    if isinstance(requested_time, str):
        hours, minutes = map(int, requested_time.split(':'))
    else:  # time object
        hours, minutes = requested_time.hour, requested_time.minute

    requested_minutes = hours * 60 + minutes

    # Преобразуем доступные времена в минуты, сохраняя оригинальное значение
    time_diffs = []
    for t in available_times:
        time_part = t.split(' ')[1] if ' ' in t else t
        h, m = map(int, time_part.split(':'))
        minutes = h * 60 + m
        # Добавляем абсолютную разницу и оригинальное время
        time_diffs.append((abs(minutes - requested_minutes), t))

    # Сортируем по абсолютной разнице
    time_diffs.sort(key=lambda x: x[0])

    # Возвращаем ближайшее время
    return time_diffs[0][1] if time_diffs else None


def round_to_nearest_half_hour(time_str):
    """
    Округляет время до ближайшего 30-минутного интервала по следующим правилам:
    - 00-15 минут → округление вниз до целого часа (9:12 → 9:00)
    - 16-45 минут → округление до получаса (9:40 → 9:30)
    - 46-59 минут → округление вверх до следующего часа (9:46 → 10:00)

    Args:
        time_str: Время в формате "HH:MM" или "HH:MM:SS" или объект datetime/time

    Returns:
        str: Округленное время в формате "HH:MM"
    """
    import re
    from datetime import datetime, time

    # Поддержка различных форматов входного времени
    if isinstance(time_str, datetime):
        hour, minute = time_str.hour, time_str.minute
    elif isinstance(time_str, time):
        hour, minute = time_str.hour, time_str.minute
    else:
        # Извлекаем часы и минуты из строки
        time_parts = re.split(r'[:\s]', str(time_str))
        hour = int(time_parts[0])
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0

    # Округление в соответствии с правилами
    if 0 <= minute <= 15:
        # Округляем вниз до начала часа
        rounded_hour, rounded_minute = hour, 0
    elif 16 <= minute <= 45:
        # Округляем до получаса
        rounded_hour, rounded_minute = hour, 30
    else:  # 46-59
        # Округляем вверх до следующего часа
        rounded_hour, rounded_minute = (hour + 1) % 24, 0

    # Форматируем результат
    return f"{rounded_hour:02d}:{rounded_minute:02d}"


def compare_times_for_redis(free_intervals, user_time, user_date):
    """
    Для Redis. Сравниваем время начала записи предпочитаемым пациенту, чтобы перезаписаться, и свободное время записей, пришедших из
    ответа сервера. Возвращает все свободные времена с датой.

    :param free_intervals: Список свободных интервалов времени
    :param user_time: Время пользователя в формате 'HH:MM:SS' или объект time
    :param user_date: Дата в формате 'YYYY-MM-DD'
    :return: Все свободные времена с датой
    """

    logger.info(f'Нахожусь в функции compare_times_for_redis\nЖелаемое время пользователя: {user_time}\n')

    # Проверяем тип user_time и преобразуем его, если это необходимо
    if isinstance(user_time, str):
        user_time_obj = datetime.strptime(user_time, '%H:%M:%S').time()
    elif isinstance(user_time, time):
        user_time_obj = user_time
    else:
        raise ValueError("user_time должен быть строкой или объектом time")

    # Собираем все доступные времена
    available_times = []

    for interval in free_intervals:
        start_time = normalize_time_for_receptions(interval['start_time'])

        # Пропускаем интервалы меньше 09:00 и больше или равно 21:00
        if start_time < time(9, 0) or start_time >= time(21, 0):
            continue

        # Пропускаем сравнение с занятым временем
        if start_time == user_time_obj:
            continue

        available_times.append(format_time_with_date(user_date, start_time))

    logger.info(f"Возвращаем все доступные времена: {len(available_times)} слотов")
    return available_times


def compare_and_suggest_times(free_intervals, user_time, user_date):
    """
    Возвращает все доступные времена на выбранную дату.
    Улучшенная версия для обработки различных форматов входных данных.

    :param free_intervals: Список свободных интервалов времени
    :param user_time: Время пользователя в формате 'HH:MM:SS' или 'HH:MM' или объект time
    :param user_date: Дата в формате 'YYYY-MM-DD'
    :return: Список всех доступных времен с датой
    """
    logger.info(f'Нахожусь в функции compare_and_suggest_times\nЖелаемое время пользователя: {user_time}\n')

    # Проверяем тип user_time и преобразуем его, если это необходимо
    user_time_obj = None
    if isinstance(user_time, str):
        try:
            # Пробуем разные форматы времени
            if ':' in user_time:
                parts = user_time.split(':')
                if len(parts) == 2:  # HH:MM
                    user_time_obj = time(int(parts[0]), int(parts[1]))
                elif len(parts) == 3:  # HH:MM:SS
                    user_time_obj = time(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                # Если не удалось распознать формат, логируем ошибку
                logger.warning(f"Некорректный формат времени: {user_time}")
                user_time_obj = time(0, 0)  # Время по умолчанию
        except ValueError as e:
            logger.warning(f"Ошибка преобразования времени: {e}, {user_time}")
            user_time_obj = time(0, 0)  # Время по умолчанию
    elif isinstance(user_time, time):
        user_time_obj = user_time
    elif isinstance(user_time, datetime):
        user_time_obj = user_time.time()
    else:
        logger.warning(f"Некорректный тип user_time: {type(user_time)}")
        user_time_obj = time(0, 0)  # Время по умолчанию

    logger.info(f"Нормализованное время пользователя: {user_time_obj}")

    # Собираем все доступные времена с проверкой формата интервалов
    available_times = []

    # Проверяем структуру free_intervals для обработки различных форматов
    if not free_intervals:
        logger.warning("Пустой список интервалов!")
        return []

    if isinstance(free_intervals, list):
        for interval in free_intervals:
            try:
                # Определяем формат интервала
                if isinstance(interval, dict) and 'start_time' in interval:
                    # Формат {'start_time': '9:00', 'end_time': '9:30'}
                    start_time_str = interval['start_time']
                elif isinstance(interval, str):
                    # Формат '9:00'
                    start_time_str = interval
                else:
                    logger.warning(f"Неизвестный формат интервала: {interval}")
                    continue

                # Нормализуем start_time
                if ':' in start_time_str:
                    parts = start_time_str.split(':')
                    if len(parts) == 2:
                        hour, minute = map(int, parts)
                        start_time = time(hour, minute)
                    else:
                        logger.warning(f"Неверный формат времени: {start_time_str}")
                        continue
                else:
                    logger.warning(f"Неверный формат времени: {start_time_str}")
                    continue

                # Проверяем время на диапазон 9:00-21:00
                if start_time < time(9, 0) or start_time >= time(21, 0):
                    continue

                # Пропускаем сравнение с занятым временем
                if start_time == user_time_obj:
                    continue

                # Форматируем и добавляем доступное время
                available_time = f"{user_date} {start_time.strftime('%H:%M')}"
                available_times.append(available_time)
            except Exception as e:
                logger.error(f"Ошибка при обработке интервала {interval}: {e}")

    # Если не удалось обработать интервалы, возвращаем пустой список
    if not available_times:
        logger.warning("Не удалось найти доступные времена")
        return []

    # Сортируем времена
    available_times.sort()

    logger.info(f"Найдено {len(available_times)} свободных времен")
    return available_times


def redis_reception_appointment(patient_id, appointment_time):
    """
    Функция выполняет резервацию времени при записи, на 15 минут в Redis для избежания конфликтов по времени,
    когда несколько клиентов хотят записаться на одно и то же время во время звонков

    :param patient_id:
    :param appointment_time:
    :return:
    """

    if redis_client.exists(appointment_time):
        logger.info('Время уже занято')
        result_redis = 0
        return result_redis
    else:
        redis_client.set(appointment_time, patient_id, ex=300)
        logger.info('Запись произведена на 5 минут')
        result_redis = 1
        return result_redis


def format_russian_date(date_obj):
    # Создаем словарь для русских названий месяцев
    months = {
        1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
        5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
        9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
    }

    # Извлекаем день и номер месяца
    day = date_obj.day
    month = date_obj.month

    # Формируем строку в нужном формате
    return f"{day} {months[month]}"


def generate_msh_10():
    return uuid.uuid4().hex


def format_doctor_name(patient_code: str) -> str:
    from reminder.models import Appointment, Patient

    try:
        patient = Patient.objects.get(patient_code=patient_code)
        appointment = Appointment.objects.filter(patient=patient).order_by('-start_time').first()

        if appointment and appointment.doctor and appointment.doctor.full_name:
            return appointment.doctor.full_name
    except Exception as e:
        logger.error(f"Ошибка в format_doctor_name: {e}")

    return "Специалист"


def select_best_doctor_for_time(patient_code, requested_date, requested_time):
    """
    Выбирает наиболее подходящего врача для запрошенного времени
    из доступных в отделении.

    Args:
        patient_code (str): Код пациента
        requested_date (str): Запрошенная дата в формате 'YYYY-MM-DD'
        requested_time (str): Запрошенное время в формате 'HH:MM'

    Returns:
        tuple: (doctor_code, clinic_id, department_id, schedident)
    """
    from reminder.infoclinica_requests.schedule.schedule_cache import get_cached_schedule

    logger.info(f"Выбор врача для пациента {patient_code} на {requested_date} {requested_time}")

    # Получаем кэшированные данные или запрашиваем заново
    cached_data = get_cached_schedule(patient_code)

    if not cached_data or 'by_doctor' not in cached_data.get('data', cached_data):
        # Нет кэша или кэш не содержит данных о врачах - делаем новый запрос
        from reminder.infoclinica_requests.schedule.doct_schedule_free import get_patient_doctor_schedule

        schedule_result = get_patient_doctor_schedule(patient_code, days_horizon=1)
        if schedule_result.get('success', False):
            cache_schedule(patient_code, schedule_result)
            cached_data = get_cached_schedule(patient_code)

    if not cached_data or 'by_doctor' not in cached_data.get('data', cached_data):
        logger.error("Не удалось получить данные о врачах")
        return None, None, None, None

    by_doctor = cached_data.get('data', cached_data)['by_doctor']
    if not by_doctor:
        logger.warning("Нет данных о врачах в кэше")
        return None, None, None, None

    # Конвертируем время запроса в минуты от начала дня
    req_hour, req_min = map(int, requested_time.split(':'))
    req_minutes = req_hour * 60 + req_min

    # Находим врачей с доступными слотами на запрашиваемую дату и время
    suitable_doctors = []

    for doc_code, doctor_data in by_doctor.items():
        for schedule in doctor_data.get('schedules', []):
            if schedule.get('date_iso') != requested_date or not schedule.get('has_free_slots', False):
                continue

            # Проверяем, имеет ли этот врач слот в указанное время
            begin_hour = schedule.get('begin_hour', 0)
            begin_min = schedule.get('begin_min', 0)
            end_hour = schedule.get('end_hour', 0)
            end_min = schedule.get('end_min', 0)

            # Преобразуем в минуты для сравнения
            begin_minutes = begin_hour * 60 + begin_min
            end_minutes = end_hour * 60 + end_min

            # Если запрошенное время попадает в интервал работы
            if begin_minutes <= req_minutes < end_minutes:
                suitable_doctors.append({
                    'doctor_code': doc_code,
                    'doctor_name': doctor_data.get('doctor_name'),
                    'department_id': doctor_data.get('department_id'),
                    'clinic_id': schedule.get('clinic_id'),
                    'schedule_id': schedule.get('schedule_id'),
                    'free_count': schedule.get('free_count', 0),
                    'time_diff': abs(req_minutes - begin_minutes)  # Насколько близко к началу рабочего дня
                })

    if not suitable_doctors:
        logger.warning(f"Не найдено подходящих врачей на {requested_date} {requested_time}")
        return None, None, None, None

    # Выбираем врача с наибольшим количеством свободных слотов или ближайшего по времени
    selected = min(suitable_doctors, key=lambda x: x['time_diff'])

    logger.info(f"Выбран врач {selected['doctor_code']} ({selected['doctor_name']}) для записи")

    return (
        selected['doctor_code'],
        selected['clinic_id'],
        selected['department_id'],
        selected['schedule_id']
    )
