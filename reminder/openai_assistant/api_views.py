import json
import logging
import re
import calendar
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient
from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
from reminder.openai_assistant.assistant_client import AssistantClient
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt
from reminder.openai_assistant.response_formatter import format_booking_response
from reminder.properties.utils import get_formatted_date_info

logger = logging.getLogger(__name__)

# Словари для перевода дат на русский и казахский
MONTHS_RU = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
    7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
}

MONTHS_KZ = {
    1: "Қаңтар", 2: "Ақпан", 3: "Наурыз", 4: "Сәуір", 5: "Мамыр", 6: "Маусым",
    7: "Шілде", 8: "Тамыз", 9: "Қыркүйек", 10: "Қазан", 11: "Қараша", 12: "Желтоқсан"
}

WEEKDAYS_RU = {
    0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"
}

WEEKDAYS_KZ = {
    0: "Дүйсенбі", 1: "Сейсенбі", 2: "Сәрсенбі", 3: "Бейсенбі", 4: "Жұма", 5: "Сенбі", 6: "Жексенбі"
}


# Функция для форматирования даты в русском/казахском формате
def format_date_info(date_obj):
    """
    Formats date in Russian and Kazakh formats

    Args:
        date_obj: Date object or datetime object

    Returns:
        dict: Formatted date information
    """
    try:
        # Ensure we're working with a datetime or date object
        if isinstance(date_obj, str):
            try:
                if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                    date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
                else:  # If date only (YYYY-MM-DD)
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
            except ValueError:
                # If parsing failed, return empty values
                return {
                    "date": "",
                    "date_kz": "",
                    "weekday": "",
                    "weekday_kz": ""
                }

        # Extract date components
        day = date_obj.day
        month_num = date_obj.month
        weekday = date_obj.weekday()

        return {
            "date": f"{day} {MONTHS_RU[month_num]}",
            "date_kz": f"{day} {MONTHS_KZ[month_num]}",
            "weekday": WEEKDAYS_RU[weekday],
            "weekday_kz": WEEKDAYS_KZ[weekday]
        }
    except Exception as e:
        logger.error(f"Error in format_date_info: {e}")
        return {
            "date": "",
            "date_kz": "",
            "weekday": "",
            "weekday_kz": ""
        }


# Функция для предобработки входных данных
def preprocess_input(text):
    """
    Нормализует текст запроса пользователя
    """
    # Приводим к нижнему регистру
    text = text.lower().strip()

    # Нормализуем упоминания дат
    text = text.replace('сегодняшний день', 'сегодня')
    text = text.replace('завтрашний день', 'завтра')
    text = text.replace('следующий день', 'завтра')

    # Исправляем опечатки
    text = text.replace('завтар', 'завтра')
    text = text.replace('завра', 'завтра')

    return text


# Функция для округления времени до ближайшего получаса
def round_to_half_hour(time_str):
    """
    Округляет время до ближайшего получаса
    """
    try:
        hour, minute = map(int, time_str.split(':'))

        # Округляем минуты до ближайшего 30
        if minute < 15:
            new_minute = 0
        elif 15 <= minute < 45:
            new_minute = 30
        else:
            new_minute = 0
            hour += 1

        # Обрабатываем переполнение часов
        if hour >= 24:
            hour = 0

        return f"{hour:02d}:{new_minute:02d}"
    except Exception as e:
        logger.error(f"Ошибка округления времени '{time_str}': {e}")
        return time_str


# Функция для определения времени суток из текста
def extract_time_of_day(text):
    """
    Извлекает время суток из текста запроса
    """
    if any(pattern in text for pattern in ["утр", "утром", "с утра", "на утро", "рано"]):
        return "10:00"
    elif any(pattern in text for pattern in ["до обеда", "перед обед"]):
        return "11:00"
    elif any(pattern in text for pattern in ["обед", "днем", "дневн", "полдень"]):
        return "13:00"
    elif any(pattern in text for pattern in ["после обеда", "дневное время"]):
        return "15:00"
    elif any(pattern in text for pattern in ["вечер", "ужин", "вечером", "поздн"]):
        return "18:00"
    return None


def extract_date_from_text(text):
    """
    Возвращает текущую дату, делегируя всю логику определения даты ассистенту.

    Функция намеренно упрощена, чтобы ассистент полностью контролировал
    интерпретацию даты и времени.
    """
    logger.info(f"Запрос на определение даты для текста: {text}")
    logger.warning("Внимание: Определение даты полностью делегировано ассистенту.")
    return None


# Функция для извлечения времени из текста
def extract_time_from_text(text):
    """
    Извлекает время из текста запроса
    """
    # Проверяем на конкретное время в формате ЧЧ:ММ или ЧЧ ММ
    time_match = re.search(r'(\d{1,2})[:\s](\d{2})', text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        return round_to_half_hour(f"{hour}:{minute}")

    # Проверяем на время суток
    time_of_day = extract_time_of_day(text)
    if time_of_day:
        return time_of_day

    # По умолчанию возвращаем утреннее время
    return "10:00"


def format_available_times_response(times, date_obj, specialist_name="Специалист", relation=None):
    """
    Форматирует ответ с доступными временами согласно требуемой структуре.

    Args:
        times: Список доступных времен
        date_obj: Объект datetime для даты
        specialist_name: Имя специалиста
        relation: Отношение к текущему дню ('today', 'tomorrow', or None)

    Returns:
        dict: Отформатированный ответ
    """
    # Если не был передан relation, определяем его
    if relation is None:
        relation = get_date_relation(date_obj)

    # Форматируем информацию о дате
    day = date_obj.day
    month_num = date_obj.month
    weekday_idx = date_obj.weekday()

    # Определяем базовый статус в зависимости от количества времен
    if len(times) == 0:
        base_status = "error_empty_windows"
    elif len(times) == 1:
        base_status = "only_first_time"
    elif len(times) == 2:
        base_status = "only_two_time"
    else:
        base_status = "which_time"

    # Добавляем суффикс _today или _tomorrow если применимо
    if relation == "today":
        status = f"{base_status}_today"
    elif relation == "tomorrow":
        status = f"{base_status}_tomorrow"
    else:
        status = base_status

    # Базовый ответ
    response = {
        "status": status,
        "date": f"{day} {MONTHS_RU[month_num]}",
        "date_kz": f"{day} {MONTHS_KZ[month_num]}",
        "specialist_name": specialist_name,
        "weekday": WEEKDAYS_RU[weekday_idx],
        "weekday_kz": WEEKDAYS_KZ[weekday_idx]
    }

    # Добавляем информацию о дне для сегодня/завтра
    if relation == "today":
        response["day"] = "сегодня"
        response["day_kz"] = "бүгін"
    elif relation == "tomorrow":
        response["day"] = "завтра"
        response["day_kz"] = "ертең"

    # Добавляем доступные времена в зависимости от их количества
    if times:
        if len(times) >= 1:
            response["first_time"] = times[0]
        if len(times) >= 2:
            response["second_time"] = times[1]
        if len(times) >= 3:
            response["third_time"] = times[2]

    # Добавляем сообщение, если нет доступных времен
    if not times:
        if relation == "today":
            response["message"] = "Свободных приемов на сегодня не найдено."
        elif relation == "tomorrow":
            response["message"] = "Свободных приемов на завтра не найдено."
        else:
            response["message"] = f"Свободных приемов на {day} {MONTHS_RU[month_num]} не найдено."

    return response


def get_date_relation(date_obj):
    """
    Определяет отношение даты к текущему дню (сегодня/завтра/другой)

    Args:
        date_obj: Объект datetime или строка даты

    Returns:
        str: 'today', 'tomorrow', или None
    """
    if isinstance(date_obj, str):
        try:
            if " " in date_obj:  # Если дата с временем (YYYY-MM-DD HH:MM)
                date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d").date()
            else:  # Если только дата (YYYY-MM-DD)
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d").date()
        except ValueError:
            return None
    elif hasattr(date_obj, 'date'):  # Если это объект datetime
        date_obj = date_obj.date()

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    if date_obj == today:
        return "today"
    elif date_obj == tomorrow:
        return "tomorrow"
    return None


def format_success_scheduling_response(time, date_obj, specialist_name, relation=None):
    """
    Formats response for successful scheduling with proper date information.

    Args:
        time: Appointment time
        date_obj: Date object
        specialist_name: Name of specialist
        relation: Relation to current day ('today', 'tomorrow', or None)

    Returns:
        dict: Formatted response
    """
    try:
        # Ensure we're working with a valid date_obj
        if isinstance(date_obj, str):
            try:
                if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                    date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
                else:  # If date only (YYYY-MM-DD)
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
            except ValueError:
                pass

        # Determine relation to today/tomorrow based on date_obj
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        if hasattr(date_obj, 'date'):
            date_only = date_obj.date()

            if date_only == today:
                relation = "today"
                status = "success_change_reception_today"
            elif date_only == tomorrow:
                relation = "tomorrow"
                status = "success_change_reception_tomorrow"
            else:
                relation = None
                status = "success_change_reception"
        else:
            status = "success_change_reception"
            if relation == "today":
                status = "success_change_reception_today"
            elif relation == "tomorrow":
                status = "success_change_reception_tomorrow"

        # Normalize time format
        if isinstance(time, str):
            # If time in format "YYYY-MM-DD HH:MM:SS"
            if " " in time and len(time) > 10:
                time = time.split(" ")[1]  # Take only time part

            # If time contains seconds (HH:MM:SS), remove them
            if time.count(":") == 2:
                time = ":".join(time.split(":")[:2])

        # Get formatted date information
        date_info = format_date_info(date_obj)

        # Base response
        response = {
            "status": status,
            "date": date_info["date"],
            "date_kz": date_info["date_kz"],
            "specialist_name": specialist_name,
            "weekday": date_info["weekday"],
            "weekday_kz": date_info["weekday_kz"],
            "time": time
        }

        # Add day information if today or tomorrow
        if relation == "today":
            response["day"] = "сегодня"
            response["day_kz"] = "бүгін"
        elif relation == "tomorrow":
            response["day"] = "завтра"
            response["day_kz"] = "ертең"

        return response
    except Exception as e:
        logger.error(f"Error in format_success_scheduling_response: {e}")
        return {
            "status": "error",
            "message": f"Ошибка при форматировании ответа об успешной записи: {str(e)}"
        }


# Функция для форматирования ответа об ошибке записи с предложением альтернатив
def format_error_scheduling_response(times, date_obj, specialist_name, relation=None):
    """
    Formats error response with alternative times.

    Args:
        times: List of alternative times
        date_obj: Date object
        specialist_name: Name of specialist
        relation: Relation to current day ('today', 'tomorrow', or None)

    Returns:
        dict: Formatted response
    """
    try:
        # Get formatted date information
        date_info = format_date_info(date_obj)

        # Determine base status based on number of times
        if len(times) == 1:
            base_status = "change_only_first_time"
        elif len(times) == 2:
            base_status = "change_only_two_time"
        else:
            base_status = "error_change_reception"

        # Add suffix _today or _tomorrow if applicable
        if relation == "today":
            status = f"{base_status}_today"
        elif relation == "tomorrow":
            status = f"{base_status}_tomorrow"
        else:
            status = base_status

        # Base response
        response = {
            "status": status,
            "date": date_info["date"],
            "date_kz": date_info["date_kz"],
            "specialist_name": specialist_name,
            "weekday": date_info["weekday"],
            "weekday_kz": date_info["weekday_kz"]
        }

        # Add day information if today or tomorrow
        if relation == "today":
            response["day"] = "сегодня"
            response["day_kz"] = "бүгін"
        elif relation == "tomorrow":
            response["day"] = "завтра"
            response["day_kz"] = "ертең"

        # Add alternative times
        if len(times) >= 1:
            response["first_time"] = times[0]
        if len(times) >= 2:
            response["second_time"] = times[1]
        if len(times) >= 3:
            response["third_time"] = times[2]

        return response
    except Exception as e:
        logger.error(f"Error in format_error_scheduling_response: {e}")
        return {
            "status": "error",
            "message": f"Ошибка при форматировании ответа с альтернативными временами: {str(e)}"
        }


def process_which_time_response(response_data, date_obj):
    """
    Обрабатывает и трансформирует ответ от функции which_time_in_certain_day.

    Args:
        response_data: Данные ответа от which_time_in_certain_day
        date_obj: Объект даты для запроса

    Returns:
        dict: Отформатированный ответ
    """
    try:
        # Преобразуем строку даты в объект datetime, если необходимо
        if isinstance(date_obj, str):
            try:
                if date_obj == "today":
                    date_obj = datetime.now()
                elif date_obj == "tomorrow":
                    date_obj = datetime.now() + timedelta(days=1)
                elif " " in date_obj:  # Если дата с временем (YYYY-MM-DD HH:MM)
                    date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
                else:  # Если только дата (YYYY-MM-DD)
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
            except ValueError:
                # Если не удалось разобрать дату, используем текущую
                date_obj = datetime.now()

        # Определяем отношение даты к текущему дню
        relation = get_date_relation(date_obj)

        # Извлекаем доступные времена
        available_times = []

        # Проверяем разные варианты полей с временами
        if "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
            available_times = response_data["all_available_times"]
        elif "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
            available_times = response_data["suggested_times"]
        else:
            # Проверяем first_time, second_time, third_time
            for key in ["first_time", "second_time", "third_time"]:
                if key in response_data and response_data[key]:
                    available_times.append(response_data[key])

            # Проверяем time_1, time_2, time_3...
            for i in range(1, 10):
                key = f"time_{i}"
                if key in response_data and response_data[key]:
                    available_times.append(response_data[key])

        # Извлекаем только время из формата "YYYY-MM-DD HH:MM"
        clean_times = []
        for t in available_times:
            if isinstance(t, str):
                if " " in t:  # Формат: "YYYY-MM-DD HH:MM"
                    clean_times.append(t.split(" ")[1])
                else:
                    # Удаляем секунды, если они есть
                    if t.count(":") == 2:  # Формат: "HH:MM:SS"
                        clean_times.append(":".join(t.split(":")[:2]))
                    else:
                        clean_times.append(t)

        # Извлекаем имя специалиста
        specialist_name = response_data.get("specialist_name",
                                            response_data.get("doctor", "Специалист"))

        # Проверяем, указывает ли response_data на отсутствие доступных слотов
        if "status" in response_data and response_data["status"].startswith("error_empty_windows"):
            # Возвращаем сообщение о отсутствии слотов без изменений
            return response_data

        # Форматируем информацию о дате
        day = date_obj.day
        month_num = date_obj.month
        weekday_idx = date_obj.weekday()

        # Определяем базовый статус в зависимости от количества времен
        if not clean_times:
            status = "error_empty_windows"
            if relation == "today":
                status = "error_empty_windows_today"
            elif relation == "tomorrow":
                status = "error_empty_windows_tomorrow"

            response = {
                "status": status,
                "message": f"Свободных приемов {'на сегодня' if relation == 'today' else 'на завтра' if relation == 'tomorrow' else ''} не найдено."
            }

            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

        elif len(clean_times) == 1:
            status = "only_first_time"
            if relation == "today":
                status = "only_first_time_today"
            elif relation == "tomorrow":
                status = "only_first_time_tomorrow"

            response = {
                "status": status,
                "date": f"{day} {MONTHS_RU[month_num]}",
                "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                "specialist_name": specialist_name,
                "weekday": WEEKDAYS_RU[weekday_idx],
                "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                "first_time": clean_times[0]
            }

            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

        elif len(clean_times) == 2:
            status = "only_two_time"
            if relation == "today":
                status = "only_two_time_today"
            elif relation == "tomorrow":
                status = "only_two_time_tomorrow"

            response = {
                "status": status,
                "date": f"{day} {MONTHS_RU[month_num]}",
                "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                "specialist_name": specialist_name,
                "weekday": WEEKDAYS_RU[weekday_idx],
                "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                "first_time": clean_times[0],
                "second_time": clean_times[1]
            }

            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

        else:  # 3 или более времен
            status = "which_time"
            if relation == "today":
                status = "which_time_today"
            elif relation == "tomorrow":
                status = "which_time_tomorrow"

            response = {
                "status": status,
                "date": f"{day} {MONTHS_RU[month_num]}",
                "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                "specialist_name": specialist_name,
                "weekday": WEEKDAYS_RU[weekday_idx],
                "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                "first_time": clean_times[0],
                "second_time": clean_times[1],
                "third_time": clean_times[2]
            }

            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

    except Exception as e:
        logger.error(f"Error in process_which_time_response: {e}", exc_info=True)
        return {
            "status": "error_med_element",
            "message": f"Ошибка при обработке ответа о доступном времени: {str(e)}"
        }


def process_reserve_reception_response(response_data, date_obj, requested_time):
    """
    Обрабатывает и трансформирует ответ от функции reserve_reception_for_patient.

    Args:
        response_data: Данные ответа от reserve_reception_for_patient
        date_obj: Объект даты для записи
        requested_time: Запрошенное время пользователем

    Returns:
        dict: Отформатированный ответ
    """
    try:
        # Определяем отношение даты к текущему дню
        relation = get_date_relation(date_obj)

        # Проверяем статус ответа
        status = response_data.get("status", "")

        # Извлекаем имя специалиста
        specialist_name = response_data.get("specialist_name", "Специалист")

        # Форматируем информацию о дате
        day = date_obj.day
        month_num = date_obj.month
        weekday_idx = date_obj.weekday()

        # Особая обработка для успешного бронирования с префиксом успешного статуса
        if status in ["success", "success_schedule", "success_change_reception"] or status.startswith(
                "success_change_reception"):
            time = response_data.get("time", requested_time)

            # Нормализуем формат времени
            if isinstance(time, str):
                # Если время в формате "YYYY-MM-DD HH:MM:SS"
                if " " in time and len(time) > 10:
                    time = time.split(" ")[1]  # Берем только часть времени

                # Если время содержит секунды (HH:MM:SS), удаляем их
                if time.count(":") == 2:
                    time = ":".join(time.split(":")[:2])

            success_status = "success_change_reception"
            if relation == "today":
                success_status = "success_change_reception_today"
            elif relation == "tomorrow":
                success_status = "success_change_reception_tomorrow"

            response = {
                "status": success_status,
                "date": f"{day} {MONTHS_RU[month_num]}",
                "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                "specialist_name": specialist_name,
                "weekday": WEEKDAYS_RU[weekday_idx],
                "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                "time": time
            }

            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

        # Если запрошенное время занято и предлагаются альтернативы
        elif status in ["suggest_times", "error_change_reception"] or status.startswith(
                "error_change_reception") or status.startswith("change_only_"):
            available_times = []

            # Проверяем разные варианты полей с временами
            if "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
                available_times = response_data["suggested_times"]
                # Извлекаем только время из формата "YYYY-MM-DD HH:MM"
                available_times = [t.split(" ")[1] if " " in t else t for t in available_times]
            elif "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
                available_times = response_data["all_available_times"]
                # Извлекаем только время из формата "YYYY-MM-DD HH:MM"
                available_times = [t.split(" ")[1] if " " in t else t for t in available_times]
            else:
                # Проверяем first_time, second_time, third_time
                for key in ["first_time", "second_time", "third_time"]:
                    if key in response_data and response_data[key]:
                        available_times.append(response_data[key])

                # Проверяем time_1, time_2, time_3...
                for i in range(1, 10):
                    key = f"time_{i}"
                    if key in response_data and response_data[key]:
                        available_times.append(response_data[key])

            # Удаляем секунды, если они есть
            clean_times = []
            for t in available_times:
                if isinstance(t, str):
                    if t.count(":") == 2:  # Формат: "HH:MM:SS"
                        clean_times.append(":".join(t.split(":")[:2]))
                    else:
                        clean_times.append(t)

            # Автоматическая попытка бронирования для первого доступного времени
            if clean_times and requested_time == "10:00" and "перенес" in response_data.get("message", "").lower():
                logger.info(f"Automatic booking attempt for first available time: {clean_times[0]}")

                # Форматируем дату и время для новой попытки
                if " " in clean_times[0]:
                    time_only = clean_times[0].split(" ")[1]
                else:
                    time_only = clean_times[0]

                new_datetime = f"{date_obj.strftime('%Y-%m-%d')} {time_only}"

                # Пытаемся сделать новое бронирование
                result = reserve_reception_for_patient(
                    patient_id=response_data.get("patient_id", ""),
                    date_from_patient=new_datetime,
                    trigger_id=1
                )

                # Обрабатываем результат
                if isinstance(result, dict):
                    if result.get("status") in ["success_schedule", "success_change_reception"] or result.get(
                            "status").startswith("success_change_reception"):
                        success_status = "success_change_reception"
                        if relation == "today":
                            success_status = "success_change_reception_today"
                        elif relation == "tomorrow":
                            success_status = "success_change_reception_tomorrow"

                        response = {
                            "status": success_status,
                            "date": f"{day} {MONTHS_RU[month_num]}",
                            "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                            "specialist_name": specialist_name,
                            "weekday": WEEKDAYS_RU[weekday_idx],
                            "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                            "time": time_only
                        }

                        if relation == "today":
                            response["day"] = "сегодня"
                            response["day_kz"] = "бүгін"
                        elif relation == "tomorrow":
                            response["day"] = "завтра"
                            response["day_kz"] = "ертең"

                        return response

            # Определяем статус ошибки в зависимости от количества альтернативных времен
            if len(clean_times) == 0:
                error_status = "error_change_reception_bad_date"
                return {
                    "status": error_status,
                    "data": response_data.get("message", "Ошибка изменения даты приема")
                }
            elif len(clean_times) == 1:
                error_status = "change_only_first_time"
                if relation == "today":
                    error_status = "change_only_first_time_today"
                elif relation == "tomorrow":
                    error_status = "change_only_first_time_tomorrow"

                response = {
                    "status": error_status,
                    "date": f"{day} {MONTHS_RU[month_num]}",
                    "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                    "specialist_name": specialist_name,
                    "weekday": WEEKDAYS_RU[weekday_idx],
                    "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                    "first_time": clean_times[0]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            elif len(clean_times) == 2:
                error_status = "change_only_two_time"
                if relation == "today":
                    error_status = "change_only_two_time_today"
                elif relation == "tomorrow":
                    error_status = "change_only_two_time_tomorrow"

                response = {
                    "status": error_status,
                    "date": f"{day} {MONTHS_RU[month_num]}",
                    "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                    "specialist_name": specialist_name,
                    "weekday": WEEKDAYS_RU[weekday_idx],
                    "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                    "first_time": clean_times[0],
                    "second_time": clean_times[1]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            else:  # 3 или более альтернативных времен
                error_status = "error_change_reception"
                if relation == "today":
                    error_status = "error_change_reception_today"
                elif relation == "tomorrow":
                    error_status = "error_change_reception_tomorrow"

                response = {
                    "status": error_status,
                    "date": f"{day} {MONTHS_RU[month_num]}",
                    "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                    "specialist_name": specialist_name,
                    "weekday": WEEKDAYS_RU[weekday_idx],
                    "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                    "first_time": clean_times[0],
                    "second_time": clean_times[1],
                    "third_time": clean_times[2]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

        # Если неверная дата
        elif status == "error_change_reception_bad_date":
            return {
                "status": "error_change_reception_bad_date",
                "data": response_data.get("message", "Ошибка изменения даты приема")
            }

        # Если нерабочее время
        elif status == "nonworktime":
            return {"status": "nonworktime"}

        # Если нет слотов
        elif status.startswith("error_empty_windows"):
            # Возвращаем ответ без изменений
            return response_data

        # Другие ошибки
        else:
            return {
                "status": "error",
                "message": response_data.get("message", "Произошла ошибка при обработке запроса")
            }
    except Exception as e:
        logger.error(f"Error in process_reserve_reception_response: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Ошибка при обработке ответа о записи/переносе: {str(e)}"
        }


# Функция для обработки ответа от функции delete_reception_for_patient
def process_delete_reception_response(response_data):
    """
    Обрабатывает и трансформирует ответ от функции delete_reception_for_patient.

    Args:
        response_data: Данные ответа от delete_reception_for_patient

    Returns:
        dict: Отформатированный ответ
    """
    try:
        # Проверяем статус ответа
        status = response_data.get("status", "")

        # Если удаление успешно
        if status == "success_delete":
            return {
                "status": "success_deleting_reception",
                "message": "Запись успешно удалена"
            }

        # Если ошибка удаления
        else:
            return {
                "status": "error_deleting_reception",
                "message": response_data.get("message", "Ошибка при удалении записи")
            }
    except Exception as e:
        logger.error(f"Error in process_delete_reception_response: {e}", exc_info=True)
        return {
            "status": "error_deleting_reception",
            "message": f"Ошибка при обработке ответа об удалении записи: {str(e)}"
        }


def process_appointment_time_response(response_data):
    """
    Обрабатывает и трансформирует ответ от функции appointment_time_for_patient.

    Args:
        response_data: Данные ответа от appointment_time_for_patient

    Returns:
        dict: Отформатированный ответ
    """
    try:
        # Проверяем, имеет ли ответ уже правильный статус
        status = response_data.get("status", "")

        # Если это статус успешного приема, просто убеждаемся, что формат правильный
        if status == "success_appointment" or status == "success_appointment_from_db":
            # Проверяем наличие всех необходимых полей
            required_fields = ["appointment_id", "appointment_time", "appointment_date", "doctor_name", "clinic_name"]
            for field in required_fields:
                if field not in response_data:
                    return {
                        "status": "error",
                        "message": f"В ответе отсутствует обязательное поле: {field}"
                    }

            # Преобразуем дату для правильного форматирования
            date_obj = None
            if "appointment_date" in response_data:
                try:
                    date_obj = datetime.strptime(response_data["appointment_date"], "%Y-%m-%d")
                except ValueError:
                    pass

            if date_obj:
                # Определяем, сегодня/завтра или другой день
                relation = get_date_relation(date_obj)

                day = date_obj.day
                month_num = date_obj.month
                weekday_idx = date_obj.weekday()

                status = "success_for_check_info"

                response = {
                    "status": status,
                    "date": f"{day} {MONTHS_RU[month_num]}",
                    "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                    "specialist_name": response_data.get("doctor_name", "Специалист"),
                    "weekday": WEEKDAYS_RU[weekday_idx],
                    "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                    "time": response_data["appointment_time"]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            # Если не удалось разобрать дату, просто возвращаем исходный ответ
            return response_data

        # Если это статус ошибки, просто возвращаем ответ
        elif status.startswith("error_"):
            return response_data

        # Для неизвестных статусов возвращаем как есть
        else:
            return response_data

    except Exception as e:
        logger.error(f"Error in process_appointment_time_response: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Ошибка при обработке ответа о текущей записи: {str(e)}"
        }


# Функция для определения намерения пользователя
# def determine_intent(user_input):
#     """
#     Определяет намерение пользователя по тексту запроса
#
#     Returns:
#         str: Одно из ["schedule", "reschedule", "check_times", "check_appointment", "delete"]
#     """
#     user_input = user_input.lower()
#
#     # Проверка на запись/перенос
#     if any(pattern in user_input for pattern in [
#         "запиш", "запис", "перенес", "перенос", "измен", "назнач", "поставь", "новое время",
#         "другое время", "другой день", "друг", "хочу на", "можно на", "поменя", "сдвинь"
#     ]):
#         # Проверяем, перенос это или новая запись
#         if any(pattern in user_input for pattern in ["перенес", "перенос", "измен", "сдвинь", "поменя"]):
#             return "reschedule"
#         else:
#             return "schedule"
#
#     # Проверка на получение информации о доступных временах
#     elif any(pattern in user_input for pattern in [
#         "свободн", "окошк", "окон", "свободное время", "доступн", "времен",
#         "когда можно", "на когда", "какое время", "какие час"
#     ]):
#         return "check_times"
#
#     # Проверка на получение информации о текущей записи
#     elif any(pattern in user_input for pattern in [
#         "когда у меня", "какое время", "когда мой", "у меня запись", "запись на",
#         "время прием", "во сколько", "на какое время", "какой день", "на какой день",
#         "не помню"
#     ]):
#         return "check_appointment"
#
#     # Проверка на удаление записи
#     elif any(pattern in user_input for pattern in [
#         "отмен", "удал", "убери", "не прид", "не смог", "отказ", "не буду",
#         "не хочу", "убер", "снять"
#     ]) and not any(pattern in user_input for pattern in [
#         "перенос", "перенес", "запиши", "запись", "записать", "назначь"
#     ]):
#         return "delete"
#
#     # По умолчанию - проверка доступных времен
#     return "check_times"


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Обработчик запросов от голосового бота, обеспечивающий правильное форматирование ответов.
    """
    try:
        # Разбор данных запроса
        data = json.loads(request.body)
        appointment_id = data.get('appointment_id')
        user_input = data.get('user_input', '').strip()

        logger.info(f"\n\n=================================================\n\n"
                    f"Processing request: "
                    f"appointment_id={appointment_id}, "
                    f"user_input='{user_input}'"
                    f"\n\n=================================================\n\n")

        if not appointment_id or not user_input:
            logger.warning("Missing required parameters")
            return JsonResponse({
                'status': 'bad_user_input',
                'message': 'Missing required parameters: appointment_id and user_input'
            })

        # Проверка наличия записи
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
            patient_code = appointment.patient.patient_code
        except Appointment.DoesNotExist:
            logger.error(f"Appointment {appointment_id} not found")
            return JsonResponse({
                'status': 'error_reception_unavailable',
                'message': 'Appointment not active or not found'
            })

        # Проверка, находится ли пациент в игнорируемом списке
        if IgnoredPatient.objects.filter(patient_code=patient_code).exists():
            logger.warning(f"Patient {patient_code} is in ignored list")
            return JsonResponse({
                'status': 'error_med_element',
                'message': 'Patient is in ignored list'
            })

        # Предварительная обработка пользовательского ввода
        user_input = preprocess_input(user_input)

        # Инициализация клиента ассистента
        assistant_client = AssistantClient()

        # Получение или создание треда для диалога
        thread = assistant_client.get_or_create_thread(appointment_id)

        # Строгие инструкции для принудительного вызова функций
        strict_instructions = """
        КРИТИЧЕСКИ ВАЖНО: ВСЕГДА ИСПОЛЬЗУЙ ФУНКЦИИ ВМЕСТО ТЕКСТОВЫХ ОТВЕТОВ!

        ТЫ ОБЯЗАН ВЫЗВАТЬ ОДНУ ИЗ ЭТИХ ФУНКЦИЙ:
        1. which_time_in_certain_day - для проверки доступных времен
        2. appointment_time_for_patient - для проверки текущей записи
        3. reserve_reception_for_patient - для записи или переноса
        4. delete_reception_for_patient - для отмены записи

        ВСЕ РЕШЕНИЯ О ДАТАХ, ВРЕМЕНИ И ДЕЙСТВИЯХ ПРИНИМАЕШЬ ТОЛЬКО ТЫ!
        НИ ПРИ КАКИХ УСЛОВИЯХ НЕ ОТВЕЧАЙ ТЕКСТОМ!
        ТОЛЬКО ВЫЗОВ ФУНКЦИИ!
        """

        # Добавление сообщения пользователя в тред
        assistant_client.add_message_to_thread(thread.thread_id, user_input)

        # Запуск ассистента с строгими инструкциями
        run = assistant_client.run_assistant(thread, appointment, instructions=strict_instructions)

        # Ожидание завершения и проверка на вызовы функций
        result = assistant_client.wait_for_run_completion(thread.thread_id, run.run_id, timeout=40)

        # Проверка наличия результатов вызова функций
        if hasattr(result, 'required_action') and result.required_action:
            # Извлечение результатов вызова функций
            tool_outputs = result.required_action.submit_tool_outputs.tool_outputs

            formatted_results = []  # Сохраним все результаты вызовов функций

            for tool_output in tool_outputs:
                try:
                    # Извлечение результата из вывода инструмента
                    function_name = tool_output.function.name
                    output_json = json.loads(tool_output.output)
                    logger.info(f"Function call result: {output_json}")

                    # Сохраняем результат
                    formatted_results.append(output_json)

                    # Обработка вывода для обеспечения правильного форматирования
                    if function_name == "which_time_in_certain_day":
                        formatted_result = process_which_time_response(output_json,
                                                                       output_json.get("date_time",
                                                                                       datetime.now().strftime(
                                                                                           "%Y-%m-%d")))
                    elif function_name == "appointment_time_for_patient":
                        formatted_result = process_appointment_time_response(output_json)
                    elif function_name == "reserve_reception_for_patient":
                        date_str = output_json.get("date_from_patient", "")
                        date_obj = None
                        time_str = "10:00"

                        if date_str:
                            try:
                                date_parts = date_str.split()
                                date_obj = datetime.strptime(date_parts[0], "%Y-%m-%d")
                                if len(date_parts) > 1:
                                    time_str = date_parts[1]
                            except ValueError:
                                date_obj = datetime.now()
                        else:
                            date_obj = datetime.now()

                        formatted_result = process_reserve_reception_response(output_json, date_obj, time_str)
                    elif function_name == "delete_reception_for_patient":
                        formatted_result = process_delete_reception_response(output_json)
                    else:
                        formatted_result = output_json

                    # Проверка и возврат правильно отформатированного ответа
                    if isinstance(formatted_result, dict) and "status" in formatted_result:
                        status_prefixes = [
                            "success_change_reception", "error_change_reception",
                            "which_time", "error_empty_windows", "only_first_time",
                            "only_two_time", "change_only_first_time", "change_only_two_time",
                            "nonworktime", "success_deleting_reception", "error_deleting_reception",
                            "error_reception_unavailable", "bad_user_input", "error_med_element",
                            "success_for_check_info"
                        ]

                        if any(formatted_result["status"].startswith(prefix) for prefix in status_prefixes):
                            logger.info(f"Returning formatted function result with status {formatted_result['status']}")
                            return JsonResponse(formatted_result)

                except Exception as e:
                    logger.error(f"Error processing tool output: {e}")

            # Если есть результаты функций, но мы не вернули ответ выше,
            # возвращаем первый результат (если он есть)
            if formatted_results:
                first_result = formatted_results[0]
                if isinstance(first_result, dict):
                    # Если в результате нет поля status, добавляем его
                    if "status" not in first_result:
                        # Определяем подходящий статус на основе содержимого
                        if "first_time" in first_result:
                            first_result["status"] = "which_time"
                        elif "time" in first_result:
                            first_result["status"] = "success_change_reception"
                        else:
                            first_result["status"] = "success_for_check_info"

                    logger.info(
                        f"Returning the first function result with status {first_result.get('status', 'unknown')}")
                    return JsonResponse(first_result)

            # Если мы дошли до этой точки, значит нужно отправить результаты функций
            # и запросить завершение запуска
            tool_output_list = []
            for tool_output in tool_outputs:
                tool_output_list.append({
                    "tool_call_id": tool_output.id,
                    "output": tool_output.output
                })

            # Отправляем результаты вызовов функций и ждем финальный ответ
            assistant_client.client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread.thread_id,
                run_id=run.run_id,
                tool_outputs=tool_output_list
            )

            # Ждем завершения выполнения
            final_result = assistant_client.wait_for_run_completion(thread.thread_id, run.run_id, timeout=30)

            # Получаем последнее сообщение ассистента после обработки функций
            latest_messages = assistant_client.get_messages(thread.thread_id, limit=1)
            if latest_messages and len(latest_messages) > 0:
                # Теперь нам нужно извлечь результат выполнения функции из метаданных запуска
                try:
                    # Ищем JSON в сообщении ассистента
                    assistant_message = latest_messages[0].content[0].text.value
                    logger.info(f"Final assistant message: {assistant_message}")

                    # Пытаемся найти JSON в сообщении
                    json_pattern = r'\{(?:[^{}]|"[^"]*"|\{(?:[^{}]|"[^"]*")*\})*\}'
                    json_matches = re.findall(json_pattern, assistant_message, re.DOTALL)

                    for json_match in json_matches:
                        try:
                            formatted_result = json.loads(json_match)
                            if isinstance(formatted_result, dict) and "status" in formatted_result:
                                status_prefixes = [
                                    "success_change_reception", "error_change_reception",
                                    "which_time", "error_empty_windows", "only_first_time",
                                    "only_two_time", "change_only_first_time", "change_only_two_time",
                                    "nonworktime", "success_deleting_reception", "error_deleting_reception"
                                ]

                                if any(formatted_result["status"].startswith(prefix) for prefix in status_prefixes):
                                    logger.info(
                                        f"Returning extracted JSON from final message with status {formatted_result['status']}")
                                    return JsonResponse(formatted_result)
                        except json.JSONDecodeError:
                            continue
                except Exception as e:
                    logger.error(f"Error handling final assistant message: {e}")

                # Если дошли до сюда, то пробуем вернуть первый результат функции снова
                if formatted_results:
                    logger.info(f"Returning first function result as backup")
                    return JsonResponse(formatted_results[0])

        # Если у нас нет результатов функций, пытаемся извлечь вызовы функций из сообщения ассистента
        messages = assistant_client.get_messages(thread.thread_id, limit=1)

        if messages and len(messages) > 0 and messages[0].role == "assistant":
            # Получаем сообщение ассистента
            assistant_message = messages[0].content[0].text.value
            logger.info(f"Assistant message: {assistant_message}")

            # Пытаемся извлечь JSON-ответ из сообщения
            try:
                json_pattern = r'\{(?:[^{}]|"[^"]*"|\{(?:[^{}]|"[^"]*")*\})*\}'
                json_matches = re.findall(json_pattern, assistant_message, re.DOTALL)

                for json_match in json_matches:
                    try:
                        formatted_result = json.loads(json_match)
                        if isinstance(formatted_result, dict) and "status" in formatted_result:
                            status_prefixes = [
                                "success_change_reception", "error_change_reception",
                                "which_time", "error_empty_windows", "only_first_time",
                                "only_two_time", "change_only_first_time", "change_only_two_time",
                                "nonworktime", "success_deleting_reception", "error_deleting_reception"
                            ]

                            if any(formatted_result["status"].startswith(prefix) for prefix in status_prefixes):
                                logger.info(f"Returning extracted JSON with status {formatted_result['status']}")
                                return JsonResponse(formatted_result)
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                logger.error(f"Error extracting JSON from response: {e}")

        # Если у нас нет результатов функций, но есть сообщение от ассистента,
        # пытаемся извлечь намерение из его текстового ответа
        messages = assistant_client.get_messages(thread.thread_id, limit=1)

        if messages and len(messages) > 0 and messages[0].role == "assistant":
            # Получаем сообщение ассистента
            assistant_message = messages[0].content[0].text.value
            logger.info(f"Assistant message: {assistant_message}")

            # Пытаемся извлечь намерение из текстового ответа
            extracted_response = extract_intent_from_assistant_message(
                assistant_message, appointment_id, patient_code
            )

            if extracted_response and isinstance(extracted_response, dict) and "status" in extracted_response:
                logger.info(f"Extracted intent from assistant message: {extracted_response['status']}")
                return JsonResponse(extracted_response)

            # Если в сообщении ассистента есть перечисление времён, это ответ на запрос о свободных окошках
            if re.search(r'- \d+:\d+', assistant_message):
                # Попробуем определить дату из сообщения
                date_match = re.search(r'на (\d+) ([а-яА-Я]+)', assistant_message)

                if date_match:
                    day = int(date_match.group(1))
                    month_name = date_match.group(2)

                    # Определяем месяц
                    month_num = datetime.now().month  # По умолчанию текущий месяц
                    for num, name in MONTHS_RU.items():
                        if name.lower().startswith(month_name.lower()) or month_name.lower().startswith(name.lower()):
                            month_num = num
                            break

                    # Создаем дату
                    try:
                        date_obj = datetime(datetime.now().year, month_num, day)
                    except ValueError:
                        # Если неверная дата, используем завтрашний день
                        date_obj = datetime.now() + timedelta(days=1)

                    # Определяем день недели
                    weekday_idx = date_obj.weekday()

                    # Извлекаем времена
                    times = re.findall(r'- (\d+:\d+)', assistant_message)

                    relation = None
                    today = datetime.now().date()
                    tomorrow = today + timedelta(days=1)

                    if date_obj.date() == today:
                        relation = "today"
                    elif date_obj.date() == tomorrow:
                        relation = "tomorrow"

                    # Форматируем ответ в соответствии с API
                    response = format_available_times_response(times, date_obj, "Специалист", relation)
                    return JsonResponse(response)

            # Пытаемся еще раз поискать JSON в сообщении ассистента
            try:
                json_pattern = r'\{(?:[^{}]|"[^"]*"|\{(?:[^{}]|"[^"]*")*\})*\}'
                json_matches = re.findall(json_pattern, assistant_message, re.DOTALL)

                for json_match in json_matches:
                    try:
                        formatted_result = json.loads(json_match)
                        if isinstance(formatted_result, dict) and "status" in formatted_result:
                            return JsonResponse(formatted_result)
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                logger.error(f"Error extracting JSON from response (second attempt): {e}")

        # Если всё иное не удалось, пробуем вызвать функцию напрямую на основе текста запроса
        if "свобод" in user_input.lower() or "окошк" in user_input.lower() or "доступн" in user_input.lower():
            # Определяем дату из запроса
            if "послезавтра" in user_input.lower() or "после завтра" in user_input.lower():
                date_obj = datetime.now() + timedelta(days=2)
            elif "завтра" in user_input.lower():
                date_obj = datetime.now() + timedelta(days=1)
            elif "сегодня" in user_input.lower():
                date_obj = datetime.now()
            else:
                # По умолчанию завтра
                date_obj = datetime.now() + timedelta(days=1)

            date_str = date_obj.strftime("%Y-%m-%d")

            # Вызываем функцию напрямую
            try:
                from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
                result = which_time_in_certain_day(patient_code, date_str)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                    processed_result = process_which_time_response(result_dict, date_obj)
                    return JsonResponse(processed_result)
                else:
                    processed_result = process_which_time_response(result, date_obj)
                    return JsonResponse(processed_result)
            except Exception as e:
                logger.error(f"Error calling which_time_in_certain_day directly: {e}", exc_info=True)

        # Если все методы не сработали, вернем базовый резервный ответ
        fallback_response = {
            "status": "error_med_element",
            "message": "Не удалось определить тип запроса"
        }

        logger.info(f"Returning fallback response with status {fallback_response['status']}")
        return JsonResponse(fallback_response)

    except json.JSONDecodeError:
        logger.error("Invalid JSON format in request")
        return JsonResponse({
            'status': 'bad_user_input',
            'message': 'Invalid JSON format'
        })
    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error_med_element',
            'message': f'Error processing request: {str(e)}'
        })


def create_assistant_with_tools(client, name: str, instructions: str, model: str = "gpt-4"):
    """
    Создает или обновляет ассистента с инструментами (tools).
    """
    if instructions is None:
        instructions = get_enhanced_assistant_prompt()
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "which_time_in_certain_day",
                "description": "Получение доступного времени на конкретный день",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reception_id": {"type": "string", "description": "ID приема"},
                        "date_time": {"type": "string", "description": "Дата YYYY-MM-DD"}
                    },
                    "required": ["reception_id", "date_time"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "appointment_time_for_patient",
                "description": "Получение текущей записи пациента",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_code": {"type": "string", "description": "Код пациента"}
                    },
                    "required": ["patient_code"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "reserve_reception_for_patient",
                "description": "Запись или перенос приема",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "ID пациента"},
                        "date_from_patient": {"type": "string", "description": "Дата приема YYYY-MM-DD HH:MM"},
                        "trigger_id": {"type": "integer", "description": "1 - запись, 2 - перенос"}
                    },
                    "required": ["patient_id", "date_from_patient", "trigger_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_reception_for_patient",
                "description": "Отмена записи пациента",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "ID пациента"}
                    },
                    "required": ["patient_id"]
                }
            }
        }
    ]

    try:
        assistants = client.beta.assistants.list(limit=100)
        existing_assistant = None

        for assistant in assistants.data:
            if assistant.name == name:
                existing_assistant = assistant
                break

        if existing_assistant:
            logger.info(f"🔄 Обновление ассистента {existing_assistant.id}...")
            updated_assistant = client.beta.assistants.update(
                assistant_id=existing_assistant.id,
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return updated_assistant
        else:
            logger.info("🆕 Создание нового ассистента...")
            new_assistant = client.beta.assistants.create(
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return new_assistant

    except Exception as e:
        logger.error(f"❌ Ошибка создания/обновления ассистента: {e}")
        raise


@csrf_exempt
@require_http_methods(["GET"])
def get_assistant_info(request):
    """
    Возвращает информацию о сохраненных ассистентах
    """
    try:
        assistants = Assistant.objects.all()
        assistants_data = [{
            'id': assistant.id,
            'assistant_id': assistant.assistant_id,
            'name': assistant.name,
            'model': assistant.model,
            'created_at': assistant.created_at.isoformat()
        } for assistant in assistants]

        return JsonResponse({
            'status': 'success',
            'assistants': assistants_data
        })
    except Exception as e:
        logger.error(f"Error getting assistants: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка получения информации об ассистентах: {str(e)}'
        }, status=500)


def preprocess_user_input(text: str) -> str:
    """
    Предварительная обработка текста запроса пользователя.

    Args:
        text: Текст запроса пользователя

    Returns:
        str: Обработанный текст
    """
    # Удаляем лишние пробелы
    text = text.strip()

    # Нормализуем упоминания дат
    text = text.lower().replace('сегодняшний день', 'сегодня')
    text = text.replace('завтрашний день', 'завтра')
    text = text.replace('следующий день', 'завтра')

    # Нормализуем упоминания времени суток
    time_replacements = {
        'в утреннее время': 'утром',
        'ранним утром': 'утром',
        'с утра пораньше': 'утром',
        'в обеденное время': 'в обед',
        'во время обеда': 'в обед',
        'ближе к обеду': 'в обед',
        'вечернее время': 'вечером',
        'поздним вечером': 'вечером',
        'ближе к вечеру': 'вечером'
    }

    for original, replacement in time_replacements.items():
        text = text.replace(original, replacement)

    return text


def try_direct_function_call(user_input: str, appointment) -> dict:
    """
    Attempts to directly determine and call the needed function for simple requests.
    Ensures responses are properly formatted according to ACS requirements.
    """
    user_input = user_input.lower()
    patient_code = appointment.patient.patient_code

    # Check for complex time expressions - delegate to assistant if found
    complex_time_expressions = [
        "после завтра", "послезавтра", "после после",
        "через неделю", "через месяц", "через день", "через два", "через 2",
        "на следующей", "следующий", "следующую", "следующее",
        "на этой", "этот", "эту", "это", "понедельник", "вторник", "среду", "четверг",
        "пятницу", "субботу", "воскресенье", "выходные", "будни", "раньше", "позже"
    ]

    if any(expr in user_input for expr in complex_time_expressions):
        return None

    # 1. Current appointment query - simple case
    if any(phrase in user_input for phrase in [
        'когда у меня запись', 'на какое время я записан', 'когда мой прием',
        'на какое время моя запись', 'когда мне приходить'
    ]):
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        logger.info("Direct function call: appointment_time_for_patient")
        result = appointment_time_for_patient(patient_code)

        # Process the response to ensure correct formatting
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
            return process_appointment_time_response(result_dict)
        return process_appointment_time_response(result)

    # 2. Cancel appointment - simple case
    if any(phrase in user_input for phrase in [
        'отмени', 'отменить', 'удали', 'удалить', 'убрать запись',
        'не хочу приходить', 'отказаться от записи'
    ]) and not any(word in user_input for word in ['перенеси', 'перенести']):
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        logger.info("Direct function call: delete_reception_for_patient")
        result = delete_reception_for_patient(patient_code)

        # Process the response to ensure correct formatting
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
            return process_delete_reception_response(result_dict)
        return process_delete_reception_response(result)

    # 3. Available times ONLY for today/tomorrow - simple case
    if any(phrase in user_input for phrase in [
        'свободные окошки', 'доступное время', 'какие времена', 'когда можно записаться',
        'доступные времена', 'свободное время', 'когда свободно'
    ]):
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day

        if 'завтра' in user_input and 'после' not in user_input and 'послезавтра' not in user_input:
            date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            logger.info(f"Direct function call: which_time_in_certain_day for tomorrow ({date})")
            result = which_time_in_certain_day(patient_code, date)

            # Process the response to ensure correct formatting
            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                return process_which_time_response(result_dict, date)
            return process_which_time_response(result, date)

        elif 'сегодня' in user_input:
            date = datetime.now().strftime("%Y-%m-%d")
            logger.info(f"Direct function call: which_time_in_certain_day for today ({date})")
            result = which_time_in_certain_day(patient_code, date)

            # Process the response to ensure correct formatting
            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                return process_which_time_response(result_dict, date)
            return process_which_time_response(result, date)
        else:
            # For uncertainty or other dates - delegate to assistant
            return None

    # For booking/rescheduling requests, we can handle simple cases
    if any(phrase in user_input for phrase in [
        'записаться на', 'запишите на', 'хочу записаться', 'перенеси на', 'перенесите на'
    ]):
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient

        # Try to extract date and time
        date_time = None

        if 'сегодня' in user_input:
            date_part = datetime.now().strftime("%Y-%m-%d")
            time_part = extract_time_from_text(user_input)
            date_time = f"{date_part} {time_part}"
        elif 'завтра' in user_input and 'после' not in user_input and 'послезавтра' not in user_input:
            date_part = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            time_part = extract_time_from_text(user_input)
            date_time = f"{date_part} {time_part}"
        else:
            # More complex date request - delegate to assistant
            return None

        if date_time:
            logger.info(f"Direct function call: reserve_reception_for_patient with {date_time}")
            result = reserve_reception_for_patient(patient_code, date_time, 1)

            # Process the response to ensure correct formatting
            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                return process_reserve_reception_response(result_dict, datetime.strptime(date_part, "%Y-%m-%d"),
                                                          time_part)
            return process_reserve_reception_response(result, datetime.strptime(date_part, "%Y-%m-%d"), time_part)

    # In other cases - delegate to assistant
    return None


def extract_intent_from_assistant_message(message, appointment_id, patient_code):
    """
    Извлекает намерение из текстового ответа ассистента и преобразует его в правильный формат API

    Args:
        message: Текстовое сообщение от ассистента
        appointment_id: ID записи
        patient_code: ID пациента

    Returns:
        dict: Отформатированный ответ в соответствии с API
    """
    try:
        # Извлекаем дату из сообщения
        date_match = re.search(r'на (\d+) ([а-яА-Я]+) \(([а-яА-Я]+)\)', message)
        if date_match:
            day = int(date_match.group(1))
            month_name = date_match.group(2)
            weekday_name = date_match.group(3)

            # Определяем номер месяца по имени
            month_num = None
            for num, name in MONTHS_RU.items():
                if name.lower() == month_name.lower():
                    month_num = num
                    break

            if month_num is None:
                # Если не нашли точное совпадение, пробуем поискать частичное
                for num, name in MONTHS_RU.items():
                    if name.lower().startswith(month_name.lower()) or month_name.lower().startswith(name.lower()):
                        month_num = num
                        break

            if month_num is None:
                # Если все еще не нашли, используем текущий месяц
                month_num = datetime.now().month

            # Определяем год (предполагаем текущий год)
            year = datetime.now().year

            # Создаем объект datetime
            try:
                date_obj = datetime(year, month_num, day)
            except ValueError:
                # Если неверная дата, используем завтрашний день
                date_obj = datetime.now() + timedelta(days=2)

            # Проверяем, является ли дата послезавтрашней
            today = datetime.now()
            day_after_tomorrow = (today + timedelta(days=2)).date()

            # Извлекаем времена из сообщения
            time_matches = re.findall(r'- (\d+:\d+)', message)
            available_times = time_matches[:3]  # Берем только первые три времени

            # Форматируем ответ в соответствии с API
            response = {
                "status": "which_time",
                "date": f"{day} {MONTHS_RU[month_num]}",
                "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                "specialist_name": "Специалист",
                "weekday": weekday_name.capitalize(),
                "weekday_kz": next((kz for ru, kz in zip(WEEKDAYS_RU.values(), WEEKDAYS_KZ.values())
                                    if ru.lower() == weekday_name.lower()), "Белгісіз күн")
            }

            # Добавляем доступные времена
            if len(available_times) >= 1:
                response["first_time"] = available_times[0]
            if len(available_times) >= 2:
                response["second_time"] = available_times[1]
            if len(available_times) >= 3:
                response["third_time"] = available_times[2]

            return response

        # Если это сообщение о текущей записи
        if "запись" in message.lower() and any(word in message.lower() for word in ["на", "в", "к", "время"]):
            # Пытаемся вызвать функцию напрямую
            from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
            result = appointment_time_for_patient(patient_code)

            if isinstance(result, dict) and "status" in result:
                return result
            elif hasattr(result, 'content'):
                return json.loads(result.content.decode('utf-8'))

            # Если не удалось получить результат, возвращаем общий ответ
            return {
                "status": "error_reception_unavailable",
                "message": "Не удалось получить информацию о записи"
            }

        # Если это сообщение о свободных окошках, но без точной даты
        if "свободн" in message.lower() and "окошк" in message.lower():
            # Используем завтрашний день по умолчанию
            tomorrow = datetime.now() + timedelta(days=1)
            date_str = tomorrow.strftime("%Y-%m-%d")

            # Вызываем функцию напрямую
            from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
            result = which_time_in_certain_day(patient_code, date_str)

            if isinstance(result, dict) and "status" in result:
                return result
            elif hasattr(result, 'content'):
                return json.loads(result.content.decode('utf-8'))

        # По умолчанию возвращаем ошибку
        return {
            "status": "error_med_element",
            "message": "Не удалось определить намерение из ответа ассистента"
        }

    except Exception as e:
        logger.error(f"Error extracting intent from assistant message: {e}", exc_info=True)
        return {
            "status": "error_med_element",
            "message": "Ошибка при анализе ответа ассистента"
        }
