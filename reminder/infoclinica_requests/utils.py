import logging
logger = logging.getLogger(__name__)
import logging
from datetime import timedelta
import os
from multiprocessing.connection import answer_challenge

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
    ответа сервера. Возвращает все свободные времена с датой вместо только трех ближайших.

    :param free_intervals: Список свободных интервалов времени
    :param user_time: Время пользователя в формате 'HH:MM:SS' или объект time
    :param user_date: Дата в формате 'YYYY-MM-DD'
    :return: Время пользователя, если есть совпадение, или все свободные времена с датой
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

    logger.info(f"Нет совпадений. Возвращаем все доступные времена: {len(available_times)} слотов")
    return available_times


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

    :param free_intervals: Список свободных интервалов времени
    :param user_time: Время пользователя в формате 'HH:MM:SS' или объект time
    :param user_date: Дата в формате 'YYYY-MM-DD'
    :return: Список всех доступных времен с датой
    """
    logger.info(f'Нахожусь в функции compare_and_suggest_times\nЖелаемое время пользователя: {user_time}\n')

    # Проверяем тип user_time и преобразуем его, если это необходимо
    if isinstance(user_time, str):
        user_time_obj = datetime.strptime(user_time, '%H:%M:%S').time()
    elif isinstance(user_time, time):
        user_time_obj = user_time
    else:
        logger.info(f"Некорректный тип user_time: {type(user_time)}")
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

    logger.info(f"Время занято. Предлагаем {len(available_times)} свободных времен:")
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


def format_doctor_name(doctor_name):
    """
    Форматирует имя врача: удаляет содержимое в скобках, оставляет только Фамилию и Имя.
    """
    if not doctor_name:
        return ""
    # Убираем содержимое в скобках
    name_without_brackets = re.sub(r'\(.*?\)', '', doctor_name).strip()
    # Разбиваем по пробелу и оставляем первые два слова (Фамилия Имя)
    name_parts = name_without_brackets.split()
    if len(name_parts) >= 2:
        return f"{name_parts[0]} {name_parts[1]}"
    return name_without_brackets


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
