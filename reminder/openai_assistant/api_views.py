# Полная реализация обработчика статусов и форматирования для reminder/openai_assistant/api_views.py

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
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt
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
    Форматирует дату в русском и казахском форматах
    """
    day = date_obj.day
    month_num = date_obj.month
    weekday = date_obj.weekday()

    return {
        "date": f"{day} {MONTHS_RU[month_num]}",
        "date_kz": f"{day} {MONTHS_KZ[month_num]}",
        "weekday": WEEKDAYS_RU[weekday],
        "weekday_kz": WEEKDAYS_KZ[weekday]
    }


# Функция для определения, является ли дата сегодняшней или завтрашней
def get_date_relation(date_obj):
    """
    Определяет отношение даты к текущему дню (сегодня/завтра/другое)
    """
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    if date_obj.date() == today:
        return "today"
    elif date_obj.date() == tomorrow:
        return "tomorrow"
    else:
        return None


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


# Функция для определения даты из текста
def extract_date_from_text(text):
    """
    Извлекает дату из текста запроса
    """
    today = datetime.now()

    # Проверяем на сегодня/завтра
    if "сегодня" in text:
        return today
    elif "завтра" in text:
        return today + timedelta(days=1)

    # Проверяем на день недели
    days_of_week = {
        "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
        "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5, "воскресенье": 6, "воскресенье": 6
    }

    for day_name, day_num in days_of_week.items():
        if day_name in text:
            # Находим следующее вхождение этого дня
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:  # Сегодня
                days_ahead = 7  # Следующая неделя
            return today + timedelta(days=days_ahead)

    # Проверяем на конкретную дату в формате DD.MM или DD/MM
    date_match = re.search(r'(\d{1,2})[./](\d{1,2})', text)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = today.year

        # Если месяц уже прошел, берем следующий год
        if month < today.month:
            year += 1
        elif month == today.month and day < today.day:
            year += 1

        try:
            return datetime(year, month, day)
        except ValueError:
            pass

    # По умолчанию возвращаем сегодня
    return today


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


# Функция для форматирования ответа со свободными временами
def format_available_times_response(times, date_obj, specialist_name, relation=None):
    """
    Форматирует ответ со свободными временами в соответствии с требуемым форматом

    Args:
        times: Список доступных времен
        date_obj: Объект даты
        specialist_name: Имя специалиста
        relation: Отношение даты к текущему дню (today/tomorrow/None)

    Returns:
        dict: Отформатированный ответ
    """
    # Получаем форматированную информацию о дате
    date_info = format_date_info(date_obj)

    # Определяем базовый статус в зависимости от количества времен
    if not times:
        base_status = "error_empty_windows"
    elif len(times) == 1:
        base_status = "only_first_time"
    elif len(times) == 2:
        base_status = "only_two_time"
    else:
        base_status = "which_time"

    # Добавляем суффикс _today или _tomorrow, если применимо
    if relation == "today":
        status = f"{base_status}_today"
    elif relation == "tomorrow":
        status = f"{base_status}_tomorrow"
    else:
        status = base_status

    # Базовый ответ
    response = {
        "status": status,
        "date": date_info["date"],
        "date_kz": date_info["date_kz"],
        "specialist_name": specialist_name,
        "weekday": date_info["weekday"],
        "weekday_kz": date_info["weekday_kz"]
    }

    # Добавляем информацию о дне, если это сегодня или завтра
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

    # Если нет времен, добавляем сообщение
    if not times:
        if relation == "today":
            response["message"] = "Свободных приемов на сегодня не найдено."
        elif relation == "tomorrow":
            response["message"] = "Свободных приемов на завтра не найдено."
        else:
            response["message"] = f"Свободных приемов на {date_info['date']} не найдено."

    return response


# Функция для форматирования ответа об успешной записи/переносе
def format_success_scheduling_response(time, date_obj, specialist_name, relation=None):
    """
    Форматирует ответ об успешной записи/переносе

    Args:
        time: Время записи
        date_obj: Объект даты
        specialist_name: Имя специалиста
        relation: Отношение даты к текущему дню (today/tomorrow/None)

    Returns:
        dict: Отформатированный ответ
    """
    # Получаем форматированную информацию о дате
    date_info = format_date_info(date_obj)

    # Определяем статус
    if relation == "today":
        status = "success_change_reception_today"
    elif relation == "tomorrow":
        status = "success_change_reception_tomorrow"
    else:
        status = "success_change_reception"

    # Базовый ответ
    response = {
        "status": status,
        "date": date_info["date"],
        "date_kz": date_info["date_kz"],
        "specialist_name": specialist_name,
        "weekday": date_info["weekday"],
        "weekday_kz": date_info["weekday_kz"],
        "time": time
    }

    # Добавляем информацию о дне, если это сегодня или завтра
    if relation == "today":
        response["day"] = "сегодня"
        response["day_kz"] = "бүгін"
    elif relation == "tomorrow":
        response["day"] = "завтра"
        response["day_kz"] = "ертең"

    return response


# Функция для форматирования ответа об ошибке записи с предложением альтернатив
def format_error_scheduling_response(times, date_obj, specialist_name, relation=None):
    """
    Форматирует ответ об ошибке записи с предложением альтернатив

    Args:
        times: Список альтернативных времен
        date_obj: Объект даты
        specialist_name: Имя специалиста
        relation: Отношение даты к текущему дню (today/tomorrow/None)

    Returns:
        dict: Отформатированный ответ
    """
    # Получаем форматированную информацию о дате
    date_info = format_date_info(date_obj)

    # Определяем базовый статус в зависимости от количества времен
    if len(times) == 1:
        base_status = "change_only_first_time"
    elif len(times) == 2:
        base_status = "change_only_two_time"
    else:
        base_status = "error_change_reception"

    # Добавляем суффикс _today или _tomorrow, если применимо
    if relation == "today":
        status = f"{base_status}_today"
    elif relation == "tomorrow":
        status = f"{base_status}_tomorrow"
    else:
        status = base_status

    # Базовый ответ
    response = {
        "status": status,
        "date": date_info["date"],
        "date_kz": date_info["date_kz"],
        "specialist_name": specialist_name,
        "weekday": date_info["weekday"],
        "weekday_kz": date_info["weekday_kz"]
    }

    # Добавляем информацию о дне, если это сегодня или завтра
    if relation == "today":
        response["day"] = "сегодня"
        response["day_kz"] = "бүгін"
    elif relation == "tomorrow":
        response["day"] = "завтра"
        response["day_kz"] = "ертең"

    # Добавляем альтернативные времена
    if len(times) >= 1:
        response["first_time"] = times[0]
    if len(times) >= 2:
        response["second_time"] = times[1]
    if len(times) >= 3:
        response["third_time"] = times[2]

    return response


# Функция для обработки ответа от функции which_time_in_certain_day
def process_which_time_response(response_data, date_obj):
    """
    Обрабатывает и преобразует ответ от функции which_time_in_certain_day
    """
    # Определяем отношение даты к текущему дню
    relation = get_date_relation(date_obj)

    # Извлекаем доступные времена
    available_times = []

    # Проверяем разные варианты полей с временами
    if "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
        available_times = response_data["all_available_times"]
    else:
        # Проверяем поля first_time, second_time, third_time
        for key in ["first_time", "second_time", "third_time"]:
            if key in response_data and response_data[key]:
                available_times.append(response_data[key])

        # Проверяем поля time_1, time_2, time_3...
        for i in range(1, 10):
            key = f"time_{i}"
            if key in response_data and response_data[key]:
                available_times.append(response_data[key])

    # Извлекаем имя специалиста
    specialist_name = response_data.get("specialist_name", "Специалист")

    # Форматируем ответ
    return format_available_times_response(available_times, date_obj, specialist_name, relation)


def process_reserve_reception_response(response_data, date_obj, requested_time):
    """
    Обрабатывает и преобразует ответ от функции reserve_reception_for_patient
    """
    # Определяем отношение даты к текущему дню
    relation = get_date_relation(date_obj)

    # Проверяем статус ответа
    status = response_data.get("status", "")

    # Извлекаем имя специалиста
    specialist_name = response_data.get("specialist_name", "Специалист")

    # Если запись успешна
    if status in ["success_schedule", "success_change_reception"]:
        time = response_data.get("time", requested_time)
        return format_success_scheduling_response(time, date_obj, specialist_name, relation)

    # Если запрошенное время занято и предлагаются альтернативы
    elif status in ["suggest_times", "error_change_reception"]:
        available_times = []

        # Проверяем разные варианты полей с временами
        if "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
            available_times = response_data["suggested_times"]
            # Извлекаем только время из формата "YYYY-MM-DD HH:MM"
            available_times = [t.split(" ")[1] if " " in t else t for t in available_times]
        else:
            # Проверяем поля first_time, second_time, third_time
            for key in ["first_time", "second_time", "third_time"]:
                if key in response_data and response_data[key]:
                    available_times.append(response_data[key])

        # Если система предложила альтернативы, но у нас был запрос без конкретного времени,
        # автоматически попробуем записать на первое доступное время
        if available_times and requested_time == "10:00" and "перенес" in response_data.get("message", "").lower():
            logger.info(f"Автоматическая попытка записи на первое доступное время: {available_times[0]}")

            # Форматируем дату и время для новой попытки
            if " " in available_times[0]:
                time_only = available_times[0].split(" ")[1]
            else:
                time_only = available_times[0]

            new_datetime = f"{date_obj.strftime('%Y-%m-%d')} {time_only}"

            # Делаем новую попытку записи
            result = reserve_reception_for_patient(
                patient_id=response_data.get("patient_id", ""),
                date_from_patient=new_datetime,
                trigger_id=1
            )

            # Обрабатываем результат
            if isinstance(result, dict):
                if result.get("status") in ["success_schedule", "success_change_reception"]:
                    return format_success_scheduling_response(time_only, date_obj, specialist_name, relation)
            elif hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                if result_dict.get("status") in ["success_schedule", "success_change_reception"]:
                    return format_success_scheduling_response(time_only, date_obj, specialist_name, relation)

        # Если автоматическая попытка не удалась или ее не было, возвращаем стандартный ответ
        return format_error_scheduling_response(available_times, date_obj, specialist_name, relation)

    # Если неверная дата
    elif status == "error_change_reception_bad_date":
        return {
            "status": "error_change_reception_bad_date",
            "data": response_data.get("message", "Неверная дата")
        }

    # Если нерабочее время
    elif status == "nonworktime":
        return {"status": "nonworktime"}

    # Прочие ошибки
    else:
        return {
            "status": "error",
            "message": response_data.get("message", "Произошла ошибка при обработке запроса")
        }


# Функция для обработки ответа от функции delete_reception_for_patient
def process_delete_reception_response(response_data):
    """
    Обрабатывает и преобразует ответ от функции delete_reception_for_patient
    """
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


# Функция для определения намерения пользователя
def determine_intent(user_input):
    """
    Определяет намерение пользователя по тексту запроса

    Returns:
        str: Одно из ["schedule", "reschedule", "check_times", "check_appointment", "delete"]
    """
    user_input = user_input.lower()

    # Проверка на запись/перенос
    if any(pattern in user_input for pattern in [
        "запиш", "запис", "перенес", "перенос", "измен", "назнач", "поставь", "новое время",
        "другое время", "другой день", "друг", "хочу на", "можно на", "поменя", "сдвинь"
    ]):
        # Проверяем, перенос это или новая запись
        if any(pattern in user_input for pattern in ["перенес", "перенос", "измен", "сдвинь", "поменя"]):
            return "reschedule"
        else:
            return "schedule"

    # Проверка на получение информации о доступных временах
    elif any(pattern in user_input for pattern in [
        "свободн", "окошк", "окон", "свободное время", "доступн", "времен",
        "когда можно", "на когда", "какое время", "какие час"
    ]):
        return "check_times"

    # Проверка на получение информации о текущей записи
    elif any(pattern in user_input for pattern in [
        "когда у меня", "какое время", "когда мой", "у меня запись", "запись на",
        "время прием", "во сколько", "на какое время", "какой день", "на какой день",
        "не помню"
    ]):
        return "check_appointment"

    # Проверка на удаление записи
    elif any(pattern in user_input for pattern in [
        "отмен", "удал", "убери", "не прид", "не смог", "отказ", "не буду",
        "не хочу", "убер", "снять"
    ]) and not any(pattern in user_input for pattern in [
        "перенос", "перенес", "запиши", "запись", "записать", "назначь"
    ]):
        return "delete"

    # По умолчанию - проверка доступных времен
    return "check_times"


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Полная реализация обработчика запросов от голосового бота
    с корректной обработкой всех статусов и форматированием ответов
    """
    try:
        # Разбор данных запроса
        data = json.loads(request.body)
        appointment_id = data.get('appointment_id')
        user_input = data.get('user_input', '').strip()

        logger.info(f"\n\n=================================================\n\n"
                    f"Обработка запроса: "
                    f"appointment_id={appointment_id}, "
                    f"user_input='{user_input}'"
                    f"\n\n=================================================\n\n")

        if not appointment_id or not user_input:
            logger.warning("Отсутствуют обязательные параметры")
            return JsonResponse({
                'status': 'error_bad_input',
                'message': 'Отсутствуют обязательные параметры: appointment_id и user_input'
            }, status=400)

        # Проверяем существование записи
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
            patient_code = appointment.patient.patient_code
        except Appointment.DoesNotExist:
            logger.error(f"Запись {appointment_id} не найдена")
            return JsonResponse({
                'status': 'error_reception_unavailable',
                'message': 'Запись не активна или не найдена'
            }, status=404)

        # Проверяем, находится ли пациент в списке игнорируемых
        if IgnoredPatient.objects.filter(patient_code=patient_code).exists():
            logger.warning(f"Пациент {patient_code} находится в списке игнорируемых")
            return JsonResponse({
                'status': 'error_ignored_patient',
                'message': f'Пациент с кодом {patient_code} находится в списке игнорируемых.'
            }, status=403)

        # Предобработка запроса пользователя
        processed_input = preprocess_input(user_input)

        # Определение намерения пользователя
        intent = determine_intent(processed_input)
        logger.info(f"Определено намерение: {intent}")

        # Специалист по умолчанию (если нет в записи)
        specialist_name = "Специалист"
        if appointment.doctor:
            specialist_name = appointment.doctor.full_name

        # Обработка в зависимости от намерения
        if intent in ["schedule", "reschedule"]:
            # Извлекаем дату и время из запроса
            date_obj = extract_date_from_text(processed_input)

            # Проверяем, указал ли пользователь конкретное время
            explicit_time = re.search(r'(\d{1,2})[:\s](\d{2})', processed_input) or any(
                keyword in processed_input for keyword in ["утр", "обед", "вечер"])

            if not explicit_time:
                # Если пользователь НЕ указал конкретное время (просто "перенеси на сегодня")
                logger.info("Пользователь не указал конкретное время, проверяем доступные времена")

                # Сначала получаем список доступных времен
                formatted_date = date_obj.strftime('%Y-%m-%d')
                available_times_result = which_time_in_certain_day(patient_code, formatted_date)

                # Преобразуем JsonResponse в dict при необходимости
                if not isinstance(available_times_result, dict) and hasattr(available_times_result, 'content'):
                    available_times_result = json.loads(available_times_result.content.decode('utf-8'))

                # Извлекаем доступные времена
                available_times = []

                # Проверяем разные варианты полей с временами
                if "all_available_times" in available_times_result and isinstance(
                        available_times_result["all_available_times"], list):
                    available_times = available_times_result["all_available_times"]
                else:
                    # Проверяем поля first_time, second_time, third_time
                    for key in ["first_time", "second_time", "third_time"]:
                        if key in available_times_result and available_times_result[key]:
                            available_times.append(available_times_result[key])

                    # Проверяем поля time_1, time_2, time_3...
                    for i in range(1, 10):
                        key = f"time_{i}"
                        if key in available_times_result and available_times_result[key]:
                            available_times.append(available_times_result[key])

                # Если есть доступные времена, выбираем первое и делаем запись
                if available_times:
                    time_str = available_times[0]
                    logger.info(f"Автоматически выбираем первое доступное время: {time_str}")

                    # Если время в формате "YYYY-MM-DD HH:MM", извлекаем только время
                    if " " in time_str:
                        time_str = time_str.split(" ")[1]
                else:
                    # Если нет доступных времен, используем значение по умолчанию
                    time_str = "10:00"
                    logger.info(f"Нет доступных времен, используем время по умолчанию: {time_str}")
            else:
                # Если пользователь указал конкретное время
                time_str = extract_time_from_text(processed_input)
                logger.info(f"Пользователь указал время: {time_str}")

            # Форматируем дату и время для API
            formatted_datetime = f"{date_obj.strftime('%Y-%m-%d')} {time_str}"
            logger.info(f"Запрос на запись/перенос на: {formatted_datetime}")

            # Вызываем функцию записи/переноса
            trigger_id = 1  # Стандартный режим записи
            result = reserve_reception_for_patient(
                patient_id=patient_code,
                date_from_patient=formatted_datetime,
                trigger_id=trigger_id
            )

            # Обрабатываем ответ
            if isinstance(result, dict):
                response = process_reserve_reception_response(result, date_obj, time_str)
            else:
                # Если result не словарь, а JsonResponse
                result_dict = json.loads(result.content.decode('utf-8'))
                response = process_reserve_reception_response(result_dict, date_obj, time_str)

            return JsonResponse(response)

        elif intent == "check_times":
            # Извлекаем дату из запроса
            date_obj = extract_date_from_text(processed_input)

            # Форматируем дату для API
            formatted_date = date_obj.strftime('%Y-%m-%d')
            logger.info(f"Запрос на проверку доступных времен на: {formatted_date}")

            # Вызываем функцию проверки доступных времен
            result = which_time_in_certain_day(patient_code, formatted_date)

            # Обрабатываем ответ
            if isinstance(result, dict):
                response = process_which_time_response(result, date_obj)
            else:
                # Если result не словарь, а JsonResponse
                result_dict = json.loads(result.content.decode('utf-8'))
                response = process_which_time_response(result_dict, date_obj)

            return JsonResponse(response)

        elif intent == "check_appointment":
            logger.info("Запрос на проверку текущей записи")

            # Вызываем функцию проверки текущей записи
            result = appointment_time_for_patient(patient_code)

            # Возвращаем ответ без изменений - он уже форматирован правильно
            if isinstance(result, dict):
                return JsonResponse(result)
            else:
                # Если result это JsonResponse
                return result

        elif intent == "delete":
            logger.info("Запрос на удаление записи")

            # Вызываем функцию удаления записи
            result = delete_reception_for_patient(patient_code)

            # Обрабатываем ответ
            if isinstance(result, dict):
                response = process_delete_reception_response(result)
            else:
                # Если result не словарь, а JsonResponse
                result_dict = json.loads(result.content.decode('utf-8'))
                response = process_delete_reception_response(result_dict)

            return JsonResponse(response)

        # Если не удалось определить намерение точно - отправляем запрос на проверку доступных времен
        today = datetime.now()
        formatted_date = today.strftime('%Y-%m-%d')
        logger.info(f"Нечеткий запрос, проверяем доступные времена на сегодня: {formatted_date}")

        result = which_time_in_certain_day(patient_code, formatted_date)

        # Обрабатываем ответ
        if isinstance(result, dict):
            response = process_which_time_response(result, today)
        else:
            # Если result не словарь, а JsonResponse
            result_dict = json.loads(result.content.decode('utf-8'))
            response = process_which_time_response(result_dict, today)

        return JsonResponse(response)

    except json.JSONDecodeError:
        logger.error("Неверный формат JSON в запросе")
        return JsonResponse({
            'status': 'error_bad_input',
            'message': 'Неверный формат JSON'
        }, status=400)
    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка обработки запроса: {str(e)}'
        }, status=500)


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
    Пытается напрямую определить и вызвать нужную функцию для определенных типов запросов.

    Args:
        user_input: Запрос пользователя
        appointment: Объект записи на прием

    Returns:
        dict: Результат вызова функции или None, если прямой вызов невозможен
    """
    user_input = user_input.lower()
    patient_code = appointment.patient.patient_code

    # Импортируем функции
    from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
    from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
    from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
    from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient

    # 1. Запрос текущей записи
    if any(phrase in user_input for phrase in [
        'когда у меня запись', 'на какое время я записан', 'когда мой прием',
        'на какое время моя запись', 'когда мне приходить'
    ]):
        logger.info("Прямой вызов функции appointment_time_for_patient")
        result = appointment_time_for_patient(patient_code)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # 2. Запрос на отмену записи
    if any(phrase in user_input for phrase in [
        'отмени', 'отменить', 'удали', 'удалить', 'убрать запись',
        'не хочу приходить', 'отказаться от записи'
    ]):
        logger.info("Прямой вызов функции delete_reception_for_patient")
        result = delete_reception_for_patient(patient_code)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # 3. Запрос доступных времен
    if any(phrase in user_input for phrase in [
        'свободные окошки', 'доступное время', 'какие времена', 'когда можно записаться',
        'доступные времена', 'свободное время', 'когда свободно'
    ]):
        logger.info("Прямой вызов функции which_time_in_certain_day")

        # Определяем, для какой даты нужны слоты
        if 'завтра' in user_input:
            date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif 'сегодня' in user_input:
            date = datetime.now().strftime("%Y-%m-%d")
        else:
            # По умолчанию - сегодня
            date = datetime.now().strftime("%Y-%m-%d")

        result = which_time_in_certain_day(patient_code, date)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # В остальных случаях используем ассистента
    return None
