import json
import logging
import calendar
import re
from datetime import datetime, timezone, timedelta, time
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv
import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone
import json
import logging
import calendar
import re
from datetime import datetime, timezone, timedelta, time
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv
import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone as django_timezone
from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient, AvailableTimeSlot, PatientDoctorAssociation
from reminder.infoclinica_requests.utils import format_doctor_name
from reminder.infoclinica_requests.utils import format_doctor_name
from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient, AvailableTimeSlot
from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
from reminder.openai_assistant.assistant_client import AssistantClient
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt, \
    get_time_selection_instructions, get_enhanced_comprehensive_instructions
from reminder.openai_assistant.helpers import check_if_time_selection_request, get_selected_time_slot
from reminder.openai_assistant.redis_conversation_context_manager import ConversationContextManager
from reminder.properties.utils import get_formatted_date_info

logger = logging.getLogger(__name__)

# Dictionaries for date formatting in Russian and Kazakh
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


def get_date_relation(date_obj):
    """
    Determines relation of date to current day (today/tomorrow/other)

    Args:
        date_obj: Date object or string (YYYY-MM-DD or YYYY-MM-DD HH:MM)

    Returns:
        str: 'today', 'tomorrow', or None
    """
    if isinstance(date_obj, str):
        try:
            if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d").date()
            else:  # If date only (YYYY-MM-DD)
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d").date()
        except ValueError:
            return None
    elif hasattr(date_obj, 'date'):  # If it's a datetime object
        date_obj = date_obj.date()

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    if date_obj == today:
        return "today"
    elif date_obj == tomorrow:
        return "tomorrow"
    return None


def format_date_info(date_obj):
    """
    Formats date in Russian and Kazakh languages.

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


def process_which_time_response(response_data, date_obj, patient_code, user_input=None):
    """
    Улучшенная функция для обработки и форматирования ответа с доступными временами с учетом контекста запроса.

    Args:
        response_data: Ответ от функции which_time_in_certain_day
        date_obj: Объект даты для запроса
        patient_code: Код пациента
        user_input: Текст запроса пользователя (для контекстной фильтрации)

    Returns:
        dict: Отформатированный ответ с учетом контекста времени суток
    """

    # Определяем соотношение даты с текущим днем
    relation = get_date_relation(date_obj)

    # Получаем имя специалиста
    specialist_name = response_data.get("specialist_name", response_data.get("doctor", None))

    # ИСПРАВЛЕНИЕ: Если имя специалиста не найдено в ответе, ищем его через модели
    if not specialist_name or specialist_name == "Специалист":
        try:
            patient = Patient.objects.get(patient_code=patient_code)

            # Ищем последнего использованного врача
            if patient.last_used_doctor:
                specialist_name = patient.last_used_doctor.full_name
                logger.info(f"Получено имя врача из last_used_doctor: {specialist_name}")
            else:
                # Ищем врача из PatientDoctorAssociation
                association = PatientDoctorAssociation.objects.filter(patient=patient).first()
                if association and association.doctor:
                    specialist_name = association.doctor.full_name
                    logger.info(f"Получено имя врача из PatientDoctorAssociation: {specialist_name}")
                else:
                    # Ищем врача из активного приема
                    appointment = Appointment.objects.filter(patient=patient, is_active=True).first()
                    if appointment and appointment.doctor:
                        specialist_name = appointment.doctor.full_name
                        logger.info(f"Получено имя врача из активного приема: {specialist_name}")
                    else:
                        # Используем format_doctor_name как последний вариант
                        specialist_name = format_doctor_name(patient_code)
                        logger.info(f"Использовано имя врача из format_doctor_name: {specialist_name}")
        except Patient.DoesNotExist:
            logger.error(f"Пациент {patient_code} не найден")
            specialist_name = "Специалист"

    # Остальной код остается без изменений...
    # Извлекаем доступные времена
    available_times = []

    # Проверяем разные форматы полей с временами
    if "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
        available_times = response_data["all_available_times"]
    elif "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
        available_times = response_data["suggested_times"]
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

    # Чистим времена (удаляем части даты, секунды)
    clean_times = []
    for t in available_times:
        if isinstance(t, str):
            if " " in t:  # Формат: "YYYY-MM-DD HH:MM"
                clean_times.append(t.split(" ")[1])
            else:
                # Удаляем секунды если есть
                if t.count(":") == 2:  # Формат: "HH:MM:SS"
                    clean_times.append(":".join(t.split(":")[:2]))
                else:
                    clean_times.append(t)

    # Применяем фильтрацию на основе контекста времени суток, если указан user_input
    if user_input:
        user_input_lower = user_input.lower()

        # Фильтрация для утра (до 12:00)
        if any(word in user_input_lower for word in ["утро", "утром", "утренн", "пораньше", "рано"]):
            clean_times = [t for t in clean_times if t < "12:00"]

        # Фильтрация для обеда/дня (12:00-16:00)
        elif any(word in user_input_lower for word in ["обед", "днём", "днем", "дневн", "полдень"]):
            clean_times = [t for t in clean_times if "12:00" <= t <= "16:00"]

        # Фильтрация для вечера (после 16:00)
        elif any(word in user_input_lower for word in ["вечер", "вечером", "вечерн", "поздно", "ужин", "поздн"]):
            clean_times = [t for t in clean_times if t >= "16:00"]

    # Дополнительная сортировка, чтобы показать наиболее релевантные времена первыми
    clean_times.sort()  # Сортируем времена

    # Проверяем статус ответа на отсутствие свободных слотов
    if "status" in response_data and response_data["status"].startswith("error_empty_windows"):
        return response_data

    # Форматируем информацию о дате
    date_info = format_date_info(date_obj)

    # Определяем статус на основе количества доступных времен
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
            "date": date_info["date"],
            "date_kz": date_info["date_kz"],
            "specialist_name": specialist_name,
            "weekday": date_info["weekday"],
            "weekday_kz": date_info["weekday_kz"],
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
            "date": date_info["date"],
            "date_kz": date_info["date_kz"],
            "specialist_name": specialist_name,
            "weekday": date_info["weekday"],
            "weekday_kz": date_info["weekday_kz"],
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

    else:  # 3 или более времён
        status = "which_time"
        if relation == "today":
            status = "which_time_today"
        elif relation == "tomorrow":
            status = "which_time_tomorrow"

        response = {
            "status": status,
            "date": date_info["date"],
            "date_kz": date_info["date_kz"],
            "specialist_name": specialist_name,
            "weekday": date_info["weekday"],
            "weekday_kz": date_info["weekday_kz"],
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


def process_reserve_reception_response(response_data, date_obj, requested_time, user_input=""):
    """
    Processes and formats response from reserve_reception_for_patient function.
    Enhanced to pass user context to the assistant for intelligent time selection.

    Args:
        response_data: Response data from reserve_reception_for_patient
        date_obj: Date object for appointment
        requested_time: Time requested by user
        user_input: Original user request text for context analysis

    Returns:
        dict: Formatted response
    """
    try:
        # Determine relation to current day
        relation = get_date_relation(date_obj)

        # Format date information
        date_info = format_date_info(date_obj)

        # Extract specialist name
        specialist_name = response_data.get("specialist_name", "Специалист")

        # Special handling for successful booking
        if response_data.get("status") in ["success", "success_schedule", "success_change_reception"] or \
                response_data.get("status", "").startswith("success_change_reception"):

            # Get time from response or use requested time
            time = response_data.get("time", requested_time)

            # Normalize time format
            if isinstance(time, str):
                # If time in format "YYYY-MM-DD HH:MM:SS"
                if " " in time and len(time) > 10:
                    time = time.split(" ")[1]  # Take only time part

                # If time contains seconds (HH:MM:SS), remove them
                if time.count(":") == 2:
                    time = ":".join(time.split(":")[:2])

            # Определение правильного статуса в зависимости от даты
            success_status = "success_change_reception"
            if relation == "today":
                success_status = "success_change_reception_today"
            elif relation == "tomorrow":
                success_status = "success_change_reception_tomorrow"

            # Сборка ответа в соответствии с документацией
            response = {
                "status": success_status,  # Используем правильный статус из списка valid_statuses
                "date": date_info["date"],
                "date_kz": date_info["date_kz"],
                "specialist_name": specialist_name,
                "weekday": date_info["weekday"],
                "weekday_kz": date_info["weekday_kz"],
                "time": time
            }

            # Add day information if needed
            if relation == "today":
                response["day"] = "сегодня"
                response["day_kz"] = "бүгін"
            elif relation == "tomorrow":
                response["day"] = "завтра"
                response["day_kz"] = "ертең"

            return response

        # If requested time is taken and alternatives are suggested
        elif response_data.get("status") in ["suggest_times", "error_change_reception"] or \
                response_data.get("status", "").startswith("error_change_reception") or \
                response_data.get("status", "").startswith("change_only_"):

            # Extract available times
            available_times = []

            # Check different field variants with times
            if "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
                available_times = response_data["suggested_times"]
            elif "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
                available_times = response_data["all_available_times"]
            else:
                # Check first_time, second_time, third_time
                for key in ["first_time", "second_time", "third_time"]:
                    if key in response_data and response_data[key]:
                        available_times.append(response_data[key])

                # Check time_1, time_2, time_3...
                for i in range(1, 10):
                    key = f"time_{i}"
                    if key in response_data and response_data[key]:
                        available_times.append(response_data[key])

            # Clean times (remove date parts, seconds)
            clean_times = []
            for t in available_times:
                if isinstance(t, str):
                    if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                        clean_times.append(t.split(" ")[1])
                    else:
                        # Remove seconds if present
                        if t.count(":") == 2:  # Format: "HH:MM:SS"
                            clean_times.append(":".join(t.split(":")[:2]))
                        else:
                            clean_times.append(t)

            # No alternative times
            if not clean_times:
                return {
                    "status": "error_change_reception_bad_date",
                    "data": response_data.get("message", "Ошибка изменения даты приема")
                }

            # Use an assistant to select the best time based on user context
            selected_time = None
            patient_id = response_data.get("patient_id", "")

            if clean_times and patient_id and user_input:
                try:
                    # Create an assistant client specifically for time selection
                    from openai import OpenAI
                    from django.conf import settings
                    client = OpenAI(api_key=settings.OPENAI_API_KEY)

                    # Create a prompt for time selection based on user context
                    prompt = f"""
                    На основе запроса пользователя: "{user_input}", 
                    выберите наиболее подходящее время из доступных: {', '.join(clean_times)}.

                    Анализируйте:
                    1. Предпочтения по времени суток (утро/день/вечер)
                    2. Любые конкретные временные ограничения
                    3. По умолчанию выбирайте самое раннее время

                    Утро: 09:00-11:59
                    День: 12:00-15:59
                    Вечер: 16:00-20:30

                    Верните ТОЛЬКО выбранное время в формате ЧЧ:ММ
                    """

                    # Get assistant's recommendation
                    response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system",
                             "content": "You are an appointment scheduling assistant that helps select the most appropriate time based on a user's request."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=20,
                        temperature=0.3
                    )

                    # Extract the selected time
                    if response.choices and len(response.choices) > 0:
                        selected_time_raw = response.choices[0].message.content.strip()
                        # Validate time format
                        import re
                        time_match = re.search(r'(\d{1,2}:\d{2})', selected_time_raw)
                        if time_match:
                            selected_time = time_match.group(1)
                        else:
                            # If assistant didn't return proper format, use first available
                            selected_time = clean_times[0]
                    else:
                        # Fallback to first available time
                        selected_time = clean_times[0]

                    # Format date-time string for booking
                    date_str = date_obj.strftime("%Y-%m-%d")
                    selected_date_time = f"{date_str} {selected_time}"

                    # Call reserve_reception_for_patient with selected time
                    from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import \
                        reserve_reception_for_patient
                    booking_result = reserve_reception_for_patient(patient_id, selected_date_time, 1)

                    # Process the booking result
                    if hasattr(booking_result, 'content'):
                        booking_result_dict = json.loads(booking_result.content.decode('utf-8'))
                    else:
                        booking_result_dict = booking_result

                    # If booking successful, return success response with correct status
                    if booking_result_dict.get("status") in ["success", "success_schedule",
                                                             "success_change_reception"] or \
                            booking_result_dict.get("status", "").startswith("success_change_reception"):

                        success_status = "success_change_reception"
                        if relation == "today":
                            success_status = "success_change_reception_today"
                        elif relation == "tomorrow":
                            success_status = "success_change_reception_tomorrow"

                        auto_book_response = {
                            "status": success_status,
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "specialist_name": specialist_name,
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "time": selected_time,
                            "auto_selected": True  # Flag that this was automatically selected
                        }

                        # Add day information if needed
                        if relation == "today":
                            auto_book_response["day"] = "сегодня"
                            auto_book_response["day_kz"] = "бүгін"
                        elif relation == "tomorrow":
                            auto_book_response["day"] = "завтра"
                            auto_book_response["day_kz"] = "ертең"

                        return auto_book_response
                except Exception as e:
                    logger.error(f"Error during intelligent time selection: {e}")
                    # Continue with standard processing if intelligent selection fails

            # If auto-booking didn't work or wasn't attempted, return alternatives as usual
            # One alternative time
            if len(clean_times) == 1:
                status = "change_only_first_time"
                if relation == "today":
                    status = "change_only_first_time_today"
                elif relation == "tomorrow":
                    status = "change_only_first_time_tomorrow"

                response = {
                    "status": status,
                    "date": date_info["date"],
                    "date_kz": date_info["date_kz"],
                    "specialist_name": specialist_name,
                    "weekday": date_info["weekday"],
                    "weekday_kz": date_info["weekday_kz"],
                    "first_time": clean_times[0]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            # Two alternative times
            elif len(clean_times) == 2:
                status = "change_only_two_time"
                if relation == "today":
                    status = "change_only_two_time_today"
                elif relation == "tomorrow":
                    status = "change_only_two_time_tomorrow"

                response = {
                    "status": status,
                    "date": date_info["date"],
                    "date_kz": date_info["date_kz"],
                    "specialist_name": specialist_name,
                    "weekday": date_info["weekday"],
                    "weekday_kz": date_info["weekday_kz"],
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

            # Three or more alternative times
            else:
                status = "error_change_reception"
                if relation == "today":
                    status = "error_change_reception_today"
                elif relation == "tomorrow":
                    status = "error_change_reception_tomorrow"

                response = {
                    "status": status,
                    "date": date_info["date"],
                    "date_kz": date_info["date_kz"],
                    "specialist_name": specialist_name,
                    "weekday": date_info["weekday"],
                    "weekday_kz": date_info["weekday_kz"],
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

        # If invalid date format
        elif response_data.get("status") == "error_change_reception_bad_date":
            return {
                "status": "error_change_reception_bad_date",
                "data": response_data.get("message", "Ошибка изменения даты приема")
            }

        # If non-working time
        elif response_data.get("status") == "nonworktime":
            return {"status": "nonworktime"}

        # If no slots available
        elif response_data.get("status", "").startswith("error_empty_windows"):
            return response_data

        # Other errors
        else:
            return {
                "status": "error_med_element",
                "message": response_data.get("message", "Произошла ошибка при обработке запроса")
            }

    except Exception as e:
        logger.error(f"Error in process_reserve_reception_response: {e}", exc_info=True)
        return {
            "status": "error_med_element",
            "message": f"Ошибка при обработке ответа о записи/переносе: {str(e)}"
        }


def process_delete_reception_response(response_data):
    """
    Processes and formats response from delete_reception_for_patient function.

    Args:
        response_data: Response data from delete_reception_for_patient

    Returns:
        dict: Formatted response
    """
    try:
        # Check response status
        status = response_data.get("status", "")

        # If deletion successful
        if status == "success_delete":
            return {
                "status": "success_deleting_reception",
                "message": "Запись успешно удалена"
            }

        # If deletion error
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


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Processes requests from the voice bot with improved handling for all edge cases
    and enhanced contextual time selection. Redis context storage ensures continuity
    between requests, even when threads are lost.
    """
    try:
        # Validate authentication
        auth_token = request.headers.get("Authorization-Token")
        expected_token = os.getenv("VOICEBOT_AUTH_TOKEN")

        if auth_token != expected_token:
            return JsonResponse({
                "status": "unauthorized",
                "message": "Неверный токен авторизации"
            }, status=401)

        # Parse request data
        data = json.loads(request.body)
        patient_code = data.get('patient_code')
        user_input = data.get('user_input', '').strip()
        delete_keyword = data.get('delete_reception_keyword')

        logger.info(f'================================='
                    f'\n\n\nReceived User Input :{user_input}\n\n\n'
                    f'=================================')

        # Initialize Redis context manager for retrieving previous context
        context_manager = ConversationContextManager()
        # Get previous context if available
        previous_context = context_manager.get_context(patient_code) or {}

        # Extract key information from previous context
        previous_date = previous_context.get('date')
        previous_day = previous_context.get('day')
        previous_times = previous_context.get('times', [])
        previous_status = previous_context.get('status')

        # Проверка условия на безопасное удаление
        if delete_keyword == "ПАРОЛЬ ДЛЯ УДАЛЕНИЯ  azsf242ffgdf":
            try:
                patient = Patient.objects.get(patient_code=patient_code)
                result = delete_reception_for_patient(patient_code)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                else:
                    result_dict = result

                response = process_delete_reception_response(result_dict)

                # Clear context after deletion
                context_manager.delete_context(patient_code)

                return JsonResponse(response)

            except Patient.DoesNotExist:
                return JsonResponse({'status': 'error_med_element', 'message': 'Пациент не найден'})
            except Exception as e:
                logger.error(f"Error during deletion with keyword: {e}")
                return JsonResponse({'status': 'bad_user_input'})

        # Validate required parameters
        if not patient_code or not user_input:
            return JsonResponse({'status': 'bad_user_input'})

        # Get patient
        try:
            patient = Patient.objects.get(patient_code=patient_code)
        except Patient.DoesNotExist:
            return JsonResponse({'status': 'error_med_element', 'message': 'Patient not found'})
        except Exception as e:
            logger.error(f"Error getting patient: {e}")
            return JsonResponse({'status': 'bad_user_input'})

        # Add direct time validation in the request handler
        time_match = re.search(r'(\d{1,2})[:\s](\d{2})', user_input)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if hour < 9 or (hour == 20 and minute > 30) or hour > 20:
                return JsonResponse({"status": "nonworktime"})

        # Initialize context for the assistant
        additional_context = {}

        # Fetch available slots for today and tomorrow to provide better context
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            day_after_tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

            # Fetch available slots for today
            today_result = which_time_in_certain_day(patient_code, today)
            if hasattr(today_result, 'content'):
                today_result = json.loads(today_result.content.decode('utf-8'))
            today_slots = extract_available_times(today_result)
            additional_context["today_slots"] = today_slots

            # Fetch available slots for tomorrow
            tomorrow_result = which_time_in_certain_day(patient_code, tomorrow)
            if hasattr(tomorrow_result, 'content'):
                tomorrow_result = json.loads(tomorrow_result.content.decode('utf-8'))
            tomorrow_slots = extract_available_times(tomorrow_result)
            additional_context["tomorrow_slots"] = tomorrow_slots

            # Store slots in database for reference
            AvailableTimeSlot.objects.filter(patient=patient).delete()

            # Store today's slots
            for time_str in today_slots:
                try:
                    time_obj = datetime.strptime(time_str, "%H:%M").time()
                    AvailableTimeSlot.objects.create(
                        patient=patient,
                        date=datetime.now().date(),
                        time=time_obj
                    )
                except ValueError:
                    continue

            # Store tomorrow's slots
            for time_str in tomorrow_slots:
                try:
                    time_obj = datetime.strptime(time_str, "%H:%M").time()
                    AvailableTimeSlot.objects.create(
                        patient=patient,
                        date=(datetime.now() + timedelta(days=1)).date(),
                        time=time_obj
                    )
                except ValueError:
                    continue

            # Check if this is a contextual time selection request based on previous context
            if previous_times and any(marker in user_input.lower() for marker in
                                      ["первое", "второе", "третье", "окошк", "первый", "второй", "третий"]):
                # Get target date from previous context
                target_date = None
                if previous_day == "today":
                    target_date = datetime.now().date()
                elif previous_day == "tomorrow":
                    target_date = (datetime.now() + timedelta(days=1)).date()
                elif previous_date:
                    # Try to parse date from previous context
                    try:
                        if "Января" in previous_date or "Февраля" in previous_date or any(
                                month in previous_date for month in MONTHS_RU.values()):
                            # Format like "29 Января" - extract day and month
                            parts = previous_date.split()
                            if len(parts) >= 2:
                                day = int(parts[0])
                                month_name = parts[1]

                                # Find month number from name
                                month_num = None
                                for num, name in MONTHS_RU.items():
                                    if name == month_name:
                                        month_num = num
                                        break

                                if month_num:
                                    # Create date using current year
                                    year = datetime.now().year
                                    target_date = datetime(year, month_num, day).date()
                        else:
                            # Try standard date format
                            target_date = datetime.strptime(previous_date, "%Y-%m-%d").date()
                    except Exception as e:
                        logger.error(f"Error parsing previous date {previous_date}: {e}")

                # Determine which time slot based on user's input
                target_time = None
                if "первое" in user_input.lower() or "первый" in user_input.lower() or "на первое" in user_input.lower():
                    if len(previous_times) >= 1:
                        target_time = previous_times[0]
                elif "второе" in user_input.lower() or "второй" in user_input.lower() or "на второе" in user_input.lower():
                    if len(previous_times) >= 2:
                        target_time = previous_times[1]
                elif "третье" in user_input.lower() or "третий" in user_input.lower() or "на третье" in user_input.lower():
                    if len(previous_times) >= 3:
                        target_time = previous_times[2]
                elif "последн" in user_input.lower():
                    if previous_times:
                        target_time = previous_times[-1]

                # If we have both target date and time, make the booking
                if target_date and target_time:
                    logger.info(f"Booking from previous context: date={target_date}, time={target_time}")
                    date_str = target_date.strftime("%Y-%m-%d")
                    datetime_str = f"{date_str} {target_time}"

                    result = reserve_reception_for_patient(patient_code, datetime_str, 1)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_reserve_reception_response(result_dict, target_date, target_time,
                                                                          user_input)

                    # Update context with new booking
                    context_manager.update_context(patient_code, {
                        'status': processed_result.get('status'),
                        'date': date_str,
                        'day': previous_day,
                        'times': []  # Clear times after booking
                    })

                    return JsonResponse(processed_result)

            # Original check using AI for time selection
            is_time_selection = check_if_time_selection_request_ai(user_input, today_slots, tomorrow_slots)
            if is_time_selection:
                target_date, target_time = get_selected_time_slot_ai(user_input, today_slots, tomorrow_slots)
                if target_date and target_time:
                    # Directly book the selected time
                    date_str = target_date.strftime("%Y-%m-%d")
                    datetime_str = f"{date_str} {target_time}"

                    result = reserve_reception_for_patient(patient_code, datetime_str, 1)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_reserve_reception_response(result_dict, target_date, target_time,
                                                                          user_input)
                    return JsonResponse(processed_result)

        except Exception as slots_error:
            logger.error(f"Error fetching available slots: {slots_error}")
            additional_context["today_slots"] = []
            additional_context["tomorrow_slots"] = []

        # Initialize assistant client
        assistant_client = AssistantClient()
        thread = assistant_client.get_or_create_thread(f"patient_{patient_code}", patient)

        # Add user message
        assistant_client.add_message_to_thread(thread.thread_id, user_input)

        # УЛУЧШЕННЫЕ инструкции для анализа запроса пользователя
        request_analysis_instructions = """
        # АНАЛИЗ ЗАПРОСА ПОЛЬЗОВАТЕЛЯ

        Проанализируй запрос пользователя и определи следующие параметры:

        1. Тип запроса:
           - booking (запись/перенос)
           - info (запрос информации о времени)
           - cancel (отмена записи)

        2. Дата запроса:
           - today (сегодня)
           - tomorrow (завтра)
           - day_after_tomorrow (послезавтра)
           - near_future (ближайшие 2-3 дня)
           - far_future (через неделю или больше)
           - specific_date (конкретная дата)

        3. Предпочтение по времени суток:
           - morning (утро, 9:00-11:59)
           - afternoon (день, 12:00-15:59)
           - evening (вечер, 16:00-20:30)
           - specific_time (конкретное время)
           - any_time (любое время)

        ## КРИТИЧЕСКИ ВАЖНО: ОБРАБОТКА ТОЧНЫХ ДАТ И ВРЕМЕННЫХ ИНТЕРВАЛОВ

        1. Если в запросе указана фраза "через X дней":
           - ВСЕГДА устанавливай date_type = "specific_date"
           - ОБЯЗАТЕЛЬНО вычисли и укажи точную дату в формате YYYY-MM-DD
           - Например, при запросе "через 4 дня" рассчитай дату путем прибавления 4 дней к текущей дате (2025-05-10 + 4 дня = 2025-05-14)

        2. Для выражений о будущем:
           - "послезавтра" = текущая дата + 2 дня (date_type = "day_after_tomorrow")
           - "после после завтра" = текущая дата + 3 дня (date_type = "specific_date" с конкретной датой)
           - "через неделю" = текущая дата + 7 дней (date_type = "specific_date" с конкретной датой)
           - "через N дней" = текущая дата + N дней (date_type = "specific_date" с конкретной датой)

        3. Если указана конкретная дата (например, "15 мая"), переведи её в формат YYYY-MM-DD с правильным годом

        4. ВАЖНО: Если дата отстоит более чем на 2 дня от текущей, используй date_type = "specific_date"

        5. Если указано конкретное время, укажи его в формате HH:MM

        Верни результат в формате JSON без дополнительных комментариев:
        ```json
        {
          "intent": "booking|info|cancel",
          "date_type": "today|tomorrow|day_after_tomorrow|near_future|far_future|specific_date",
          "specific_date": "YYYY-MM-DD или null",
          "time_preference": "morning|afternoon|evening|specific_time|any_time",
          "specific_time": "HH:MM или null"
        }
        ```

        ПРИМЕРЫ ПРАВИЛЬНОГО АНАЛИЗА:

        1. "Запиши на сегодня в 15:00"
        ```json
        {
          "intent": "booking",
          "date_type": "today",
          "specific_date": null,
          "time_preference": "specific_time",
          "specific_time": "15:00"
        }
        ```

        2. "Запиши через 4 дня"
        ```json
        {
          "intent": "booking",
          "date_type": "specific_date",
          "specific_date": "2025-05-14",
          "time_preference": "any_time",
          "specific_time": null
        }
        ```

        3. "Запиши после послезавтра утром"
        ```json
        {
          "intent": "booking",
          "date_type": "specific_date", 
          "specific_date": "2025-05-13",
          "time_preference": "morning",
          "specific_time": null
        }
        ```

        4. "Запиши через неделю вечером"
        ```json
        {
          "intent": "booking",
          "date_type": "specific_date",
          "specific_date": "2025-05-17",
          "time_preference": "evening",
          "specific_time": null
        }
        ```
        """

        # Запрос к ассистенту для анализа намерения пользователя
        analysis_thread = assistant_client.get_or_create_thread(f"analysis_{patient_code}", patient)
        assistant_client.add_message_to_thread(analysis_thread.thread_id,
                                               f"Запрос пользователя: '{user_input}'. Проанализируй и определи намерение, дату и предпочтение по времени.")

        analysis_run = assistant_client.run_assistant(
            analysis_thread,
            patient,
            instructions=request_analysis_instructions
        )

        # Ждем результат анализа
        try:
            analysis_result = assistant_client.wait_for_run_completion(
                analysis_thread.thread_id,
                analysis_run.run_id,
                timeout=5
            )

            # Извлекаем результат анализа из ответа ассистента
            analysis_messages = assistant_client.get_messages(analysis_thread.thread_id, limit=1)
            analysis_data = extract_json_from_message(analysis_messages[0])

            if not analysis_data:
                analysis_data = {
                    "intent": "info",  # По умолчанию считаем, что пользователь запрашивает информацию
                    "date_type": "today",
                    "specific_date": None,
                    "time_preference": "any_time",
                    "specific_time": None
                }

            logger.info(f"Анализ запроса: {analysis_data}")

        except Exception as analysis_error:
            logger.error(f"Ошибка при анализе запроса: {analysis_error}")
            analysis_data = {
                "intent": "info",
                "date_type": "today",
                "specific_date": None,
                "time_preference": "any_time",
                "specific_time": None
            }

        # Упрощенная обработка даты, полагаясь на данные от ассистента
        if analysis_data.get("date_type") == "specific_date" and analysis_data.get("specific_date"):
            # Используем конкретную дату из анализа
            try:
                request_date = datetime.strptime(analysis_data.get("specific_date"), "%Y-%m-%d")

                # Определяем, является ли это дальней датой (более 2 дней)
                days_difference = (request_date.date() - datetime.now().date()).days
                is_far_future = days_difference > 2
                logger.info(
                    f"Определена дата из specific_date: {analysis_data.get('specific_date')}, разница {days_difference} дней, дальняя дата: {is_far_future}")
            except ValueError:
                logger.error(f"Ошибка преобразования даты {analysis_data.get('specific_date')}")
                request_date = datetime.now()
                is_far_future = False
        else:
            # Используем стандартную логику для других типов дат
            if analysis_data.get("date_type") == "tomorrow":
                request_date = datetime.now() + timedelta(days=1)
                is_far_future = False
            elif analysis_data.get("date_type") == "day_after_tomorrow":
                request_date = datetime.now() + timedelta(days=2)
                is_far_future = False
            elif analysis_data.get("date_type") == "near_future":
                # Для near_future без конкретной даты берем +3 дня по умолчанию
                request_date = datetime.now() + timedelta(days=3)
                is_far_future = True
            elif analysis_data.get("date_type") == "far_future":
                # Для far_future без конкретной даты берем +7 дней по умолчанию
                request_date = datetime.now() + timedelta(days=7)
                is_far_future = True
            else:
                # По умолчанию сегодня
                request_date = datetime.now()
                is_far_future = False

        # Дополнительно проверяем текст на наличие "через X дней"
        days_pattern = re.search(r'через\s+(\d+)\s+(?:дн[еяй]|день|дня)', user_input.lower())
        if days_pattern:
            days_count = int(days_pattern.group(1))
            request_date = datetime.now() + timedelta(days=days_count)
            is_far_future = days_count > 2
            logger.info(
                f"Найдена фраза 'через {days_count} дней', разница {days_count} дней, дальняя дата: {is_far_future}")

        # Для каждого типа запроса выполняем соответствующие действия

        # 1. Для отмены записи
        if analysis_data.get("intent") == "cancel":
            result = delete_reception_for_patient(patient_code)
            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
            else:
                result_dict = result

            response = process_delete_reception_response(result_dict)

            # Clear context after deletion
            context_manager.delete_context(patient_code)

            return JsonResponse(response)

        # 2. Для запроса информации о доступных временах
        elif analysis_data.get("intent") == "info":
            date_str = request_date.strftime("%Y-%m-%d")

            result = which_time_in_certain_day(patient_code, date_str)
            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
            else:
                result_dict = result

            response = process_which_time_response(result_dict, request_date, patient_code, user_input)

            # Update context with shown times
            times = []
            for field in ["first_time", "second_time", "third_time"]:
                if field in response and response[field]:
                    times.append(response[field])

            # Determine day type
            day_type = None
            days_diff = (request_date.date() - datetime.now().date()).days
            if days_diff == 0:
                day_type = "today"
            elif days_diff == 1:
                day_type = "tomorrow"

            context_manager.update_context(patient_code, {
                'status': response.get('status'),
                'times': times,
                'date': date_str,
                'day': day_type
            })

            return JsonResponse(response)

        # 3. Для запроса на запись/перенос
        # Для запросов на запись без конкретного времени
        elif analysis_data.get("intent") == "booking":
            # КЛЮЧЕВАЯ ПРОВЕРКА - если это дальняя дата, показываем только доступные времена
            if is_far_future:  # На дату более чем через 2 дня
                # Только показываем доступные времена без автоматического бронирования
                date_str = request_date.strftime("%Y-%m-%d")
                logger.info(f"Запрос доступных времен для дальней даты: {date_str}")

                result = which_time_in_certain_day(patient_code, date_str)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                else:
                    result_dict = result

                response = process_which_time_response(result_dict, request_date, patient_code)

                # Update context with shown times
                times = []
                for field in ["first_time", "second_time", "third_time"]:
                    if field in response and response[field]:
                        times.append(response[field])

                # Determine day type
                day_type = None
                days_diff = (request_date.date() - datetime.now().date()).days
                if days_diff == 0:
                    day_type = "today"
                elif days_diff == 1:
                    day_type = "tomorrow"

                context_manager.update_context(patient_code, {
                    'status': response.get('status'),
                    'times': times,
                    'date': date_str,
                    'day': day_type
                })

                return JsonResponse(response)

            else:  # сегодня, завтра, послезавтра - автоматически бронируем
                # Получаем доступные слоты
                date_str = request_date.strftime("%Y-%m-%d")
                available_slots_result = which_time_in_certain_day(patient_code, date_str)

                # Преобразуем результат в словарь
                if hasattr(available_slots_result, 'content'):
                    available_slots_dict = json.loads(available_slots_result.content.decode('utf-8'))
                else:
                    available_slots_dict = available_slots_result

                # Извлекаем времена
                available_times = extract_available_times(available_slots_dict)

                if available_times:
                    # КЛЮЧЕВАЯ МОДИФИКАЦИЯ: Используем OpenAI GPT для выбора времени
                    try:
                        from openai import OpenAI
                        client = OpenAI(api_key=settings.OPENAI_API_KEY)

                        # Формируем запрос для ассистента
                        time_selection_prompt = f"""
                        На основе запроса пользователя: "{user_input}"
                        выбери наиболее подходящее время из доступных: {available_times}

                        ВАЖНЫЙ КОНТЕКСТ:
                        {f"В предыдущем ответе пользователю были показаны: {previous_times}" if previous_times else ""}
                        {f"Предыдущий контекст содержал дату: {previous_date}" if previous_date else ""}
                        {f"Предыдущий статус ответа: {previous_status}" if previous_status else ""}

                        Определи из запроса пользователя, какое время суток он предпочитает:
                        - Утро (9:00-11:59)
                        - День (12:00-15:59)
                        - Вечер (16:00-20:30)

                        Если в запросе указано конкретное время, выбери ближайшее к нему из доступных.
                        Если в запросе указано время суток (утро/день/вечер), выбери подходящее из этого периода.
                        Если нет прямого указания на время, выбери самое раннее доступное.

                        Верни ТОЛЬКО выбранное время в формате ЧЧ:ММ без каких-либо пояснений.
                        """

                        # Отправляем запрос к ассистенту
                        response = client.chat.completions.create(
                            model="gpt-4",
                            messages=[
                                {"role": "system",
                                 "content": "You are a helpful assistant that selects the most appropriate time slot based on user's preferences."},
                                {"role": "user", "content": time_selection_prompt}
                            ],
                            max_tokens=20,
                            temperature=0.2
                        )

                        # Извлекаем ответ ассистента
                        time_response = response.choices[0].message.content.strip()

                        # Проверяем, соответствует ли ответ формату времени
                        time_match = re.search(r'(\d{1,2}):(\d{2})', time_response)
                        if time_match:
                            # Форматируем время в правильный формат
                            hour = int(time_match.group(1))
                            minute = int(time_match.group(2))
                            selected_time = f"{hour:02d}:{minute:02d}"

                            # Проверяем, есть ли выбранное время в списке доступных
                            if selected_time in available_times:
                                logger.info(f"Ассистент выбрал время: {selected_time}")
                            else:
                                # Ищем ближайшее время из доступных
                                selected_datetime = datetime.strptime(selected_time, "%H:%M")
                                closest_time = min(available_times, key=lambda x:
                                abs((datetime.strptime(x, "%H:%M") - selected_datetime).total_seconds()))
                                selected_time = closest_time
                                logger.info(f"Ассистент выбрал ближайшее доступное время: {selected_time}")
                        else:
                            # Если ассистент не вернул корректное время, используем первое доступное
                            selected_time = available_times[0]
                            logger.info(f"Используем первое доступное время: {selected_time}")

                        # Записываем на выбранное время
                        datetime_str = f"{date_str} {selected_time}"
                        booking_result = reserve_reception_for_patient(patient_code, datetime_str, 1)

                        # Обрабатываем результат
                        if hasattr(booking_result, 'content'):
                            booking_result_dict = json.loads(booking_result.content.decode('utf-8'))
                        else:
                            booking_result_dict = booking_result

                        processed_result = process_reserve_reception_response(
                            booking_result_dict,
                            request_date,
                            selected_time,
                            user_input
                        )

                        # Обновляем контекст
                        day_type = None
                        if "today" in processed_result.get("status", ""):
                            day_type = "today"
                        elif "tomorrow" in processed_result.get("status", ""):
                            day_type = "tomorrow"

                        context_manager.update_context(patient_code, {
                            'status': processed_result.get('status'),
                            'date': date_str,
                            'day': day_type,
                            'booked_time': selected_time,
                            'times': []  # Очищаем предыдущие времена
                        })

                        return JsonResponse(processed_result)

                    except Exception as e:
                        logger.error(f"Ошибка при использовании ассистента для выбора времени: {e}")
                        # Если произошла ошибка, используем стандартный подход - первое доступное время
                        selected_time = available_times[0]

                        datetime_str = f"{date_str} {selected_time}"
                        booking_result = reserve_reception_for_patient(patient_code, datetime_str, 1)

                        if hasattr(booking_result, 'content'):
                            booking_result_dict = json.loads(booking_result.content.decode('utf-8'))
                        else:
                            booking_result_dict = booking_result

                        processed_result = process_reserve_reception_response(
                            booking_result_dict,
                            request_date,
                            selected_time,
                            user_input
                        )

                        return JsonResponse(processed_result)

                if not available_times:
                    # Нет доступных времен
                    return JsonResponse({
                        "status": "error_empty_windows",
                        "message": f"На {date_str} нет доступных времен для записи"
                    })

        # Create context-enhanced instructions with previous interaction data
        context_instructions = """
        # КРИТИЧЕСКИ ВАЖНАЯ ИНФОРМАЦИЯ О ПРЕДЫДУЩЕМ КОНТЕКСТЕ
        """

        if previous_times:
            context_instructions += f"""
            ## В предыдущем ответе пользователю были показаны следующие времена:
            """

            # Add each time with its ordinal position for clarity
            for i, time in enumerate(previous_times):
                position = "первое" if i == 0 else "второе" if i == 1 else "третье" if i == 2 else f"{i + 1}-е"
                context_instructions += f"""
                {position} время: {time}
                """

            context_instructions += f"""

            ## ПРАВИЛА ОБРАБОТКИ ССЫЛОК НА ПРЕДЫДУЩИЕ ВРЕМЕНА:

            КРИТИЧЕСКИ ВАЖНО: Если пользователь ссылается на времена из предыдущего контекста:

            1. Если пользователь говорит "первое время", "первый вариант", "на первое", "первое" → ему нужно записаться на {previous_times[0] if len(previous_times) > 0 else '(нет данных)'}
            2. Если пользователь говорит "второе время", "второй вариант", "на второе", "второе" → ему нужно записаться на {previous_times[1] if len(previous_times) > 1 else '(нет данных)'}
            3. Если пользователь говорит "третье время", "третий вариант", "на третье", "третье" → ему нужно записаться на {previous_times[2] if len(previous_times) > 2 else '(нет данных)'}
            4. Если пользователь пишет что-то вроде "а давай на первое окошко" → ему нужно записаться на {previous_times[0] if len(previous_times) > 0 else '(нет данных)'}

            ВАЖНО: Если в запросе пользователя упоминается "первое", "второе", "третье" время, или "окошко" - это ВСЕГДА ссылка на времена, показанные в предыдущем ответе.
            """

        if previous_date or previous_day:
            context_instructions += f"""

            ## КРИТИЧЕСКИ ВАЖНО: Информация о дате из предыдущего контекста:

            Дата: {previous_date or "Не указана"}
            День: {previous_day or "Не указан"}

            КРИТИЧЕСКИ ВАЖНО: Используй именно ту ДАТУ, которая была в предыдущем ответе.
            НЕ используй текущую дату, если в предыдущем ответе была другая дата!
            """

        # Обработка прочих запросов через общий механизм ассистента
        comprehensive_instructions = get_enhanced_comprehensive_instructions(
            user_input=user_input,
            patient_code=patient_code,
            thread_id=thread.thread_id,
            assistant_client=assistant_client
        )

        # Добавляем информацию о доступных слотах
        available_slots_context = format_available_slots_for_prompt(
            patient,
            datetime.now().date(),
            (datetime.now() + timedelta(days=1)).date()
        )

        # Добавляем инструкции о различии между ближними и дальними датами
        date_booking_instructions = """
        ## ПРАВИЛА БРОНИРОВАНИЯ В ЗАВИСИМОСТИ ОТ ДАТЫ

        ### КРИТИЧЕСКИ ВАЖНО: ОБРАБОТКА ДАТ
        1. ВСЕГДА проверяй, есть ли в запросе указание на конкретную дату
        2. Специальные фразы "через X дней" ВСЕГДА обрабатывай как конкретные даты
        3. ВСЕГДА вычисляй конкретную дату, прибавляя нужное количество дней к текущей

        ### ДЛЯ БЛИЖНИХ ДАТ (СЕГОДНЯ, ЗАВТРА, ПОСЛЕЗАВТРА):
        Для записи на ближайшие 2 дня:
        1. СНАЧАЛА вызови which_time_in_certain_day для получения доступных времен
        2. ВЫБЕРИ время из списка доступных, что наиболее соответствует запросу
        3. ВЫЗОВИ reserve_reception_for_patient с выбранным временем

        ### ДЛЯ ДАЛЬНИХ ДАТ (БОЛЕЕ ЧЕМ ЧЕРЕЗ 2 ДНЯ):
        Для даты через 3 и более дней ОБЯЗАТЕЛЬНО:
        1. Вызови which_time_in_certain_day с правильной датой
        2. НЕ ВЫЗЫВАЙ reserve_reception_for_patient автоматически!
        3. ОСТАНОВИСЬ после получения списка времен
        4. Верни ответ со статусом which_time для показа пользователю

        ### ПРИМЕРЫ ПРЕОБРАЗОВАНИЯ ДАТ:
        - "через 4 дня" = сегодня + 4 дня = конкретная дата (например, 2025-05-14)
        - "через неделю" = сегодня + 7 дней = конкретная дата (например, 2025-05-17)
        - "послезавтра" = сегодня + 2 дня = конкретная дата (например, 2025-05-12)

        ### ВАЖНО: Для выражения "через X дней", где X > 2, НИКОГДА не бронируй автоматически!
        """

        # Объединяем все инструкции
        final_instructions = (comprehensive_instructions + "\n\n" +
                              context_instructions + "\n\n" +
                              date_booking_instructions + "\n\n" +
                              available_slots_context)

        # Запускаем ассистента
        run = assistant_client.run_assistant(thread, patient, instructions=final_instructions)

        # Ждем ответ
        result = assistant_client.wait_for_run_completion(thread.thread_id, run.run_id, timeout=15)

        # Проверяем валидность результата
        valid_statuses = [
            "success_change_reception", "success_change_reception_today", "success_change_reception_tomorrow",
            "error_change_reception", "error_change_reception_today", "error_change_reception_tomorrow",
            "which_time", "which_time_today", "which_time_tomorrow",
            "error_empty_windows", "error_empty_windows_today", "error_empty_windows_tomorrow",
            "nonworktime", "error_med_element", "no_action_required",
            "success_deleting_reception", "error_deleting_reception", "error_change_reception_bad_date",
            "only_first_time_tomorrow", "only_first_time_today", "only_first_time",
            "only_two_time_tomorrow", "only_two_time_today", "only_two_time",
            "change_only_first_time_tomorrow", "change_only_first_time_today", "change_only_first_time",
            "change_only_two_time_tomorrow", "change_only_two_time_today", "change_only_two_time",
            "bad_user_input"
        ]

        if isinstance(result, dict) and "status" in result:
            if result["status"] in valid_statuses:
                # Update context from result
                times = []
                for field in ["first_time", "second_time", "third_time"]:
                    if field in result and result[field]:
                        times.append(result[field])

                day_type = None
                if "today" in result.get("status", ""):
                    day_type = "today"
                elif "tomorrow" in result.get("status", ""):
                    day_type = "tomorrow"

                # Get date from result if available
                result_date = None
                if "date" in result:
                    # Try to parse date from Russian format to YYYY-MM-DD
                    try:
                        date_parts = result["date"].split()
                        if len(date_parts) >= 2:
                            day = int(date_parts[0])
                            month_name = date_parts[1]

                            month_num = None
                            for num, name in MONTHS_RU.items():
                                if name == month_name:
                                    month_num = num
                                    break

                            if month_num:
                                year = datetime.now().year
                                result_date = datetime(year, month_num, day).strftime("%Y-%m-%d")
                    except Exception as e:
                        logger.error(f"Error parsing date from result: {e}")

                # Update Redis context
                context_data = {
                    'status': result.get('status'),
                    'times': times
                }

                if day_type:
                    context_data['day'] = day_type

                if result_date:
                    context_data['date'] = result_date

                context_manager.update_context(patient_code, context_data)

                return JsonResponse(result)
            else:
                logger.warning(f"Invalid status received from assistant: {result.get('status')}")
                fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                return JsonResponse(fallback_response)

        # Если не получен валидный результат
        logger.warning(f"Assistant did not return a valid response for user input: {user_input}")
        fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
        return JsonResponse(fallback_response)

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JsonResponse({'status': 'bad_user_input'})


def extract_json_from_message(message):
    """
    Extracts JSON data from assistant message.

    Args:
        message: Message object from assistant

    Returns:
        dict: Extracted JSON data or None
    """
    try:
        if not message or not hasattr(message, 'content'):
            return None

        content_text = ""
        for content_item in message.content:
            if hasattr(content_item, 'text') and content_item.text:
                content_text += content_item.text.value

        # Look for JSON in backticks
        json_match = re.search(r'```(?:json)?\s*({[\s\S]*?})\s*```', content_text)
        if json_match:
            json_str = json_match.group(1)
            return json.loads(json_str)

        # If no JSON in backticks, try to find JSON anywhere in the text
        json_match = re.search(r'({[\s\S]*})', content_text)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except:
                pass

        return None
    except Exception as e:
        logger.error(f"Error extracting JSON from message: {e}")
        return None


def extract_text_content(message):
    """
    Extracts plain text from assistant message.

    Args:
        message: Message object from assistant

    Returns:
        str: Extracted text
    """
    try:
        if not message or not hasattr(message, 'content'):
            return ""

        content_text = ""
        for content_item in message.content:
            if hasattr(content_item, 'text') and content_item.text:
                content_text += content_item.text.value

        return content_text
    except Exception as e:
        logger.error(f"Error extracting text content: {e}")
        return ""


def get_date_from_analysis(analysis_data, user_input=""):
    """
    Улучшенная функция для определения даты из анализа и текста запроса.

    Args:
        analysis_data: Данные анализа запроса
        user_input: Оригинальный текст запроса пользователя

    Returns:
        datetime: Объект даты
    """
    try:
        today = datetime.now()

        # Проверяем особые фразы в запросе
        user_input_lower = user_input.lower()
        if "после после завтра" in user_input_lower or "послепослезавтра" in user_input_lower:
            return today + timedelta(days=3)
        elif "послезавтра" in user_input_lower or "после завтра" in user_input_lower:
            return today + timedelta(days=2)

        # Обработка из analysis_data
        if analysis_data.get("date_type") == "tomorrow":
            return today + timedelta(days=1)
        elif analysis_data.get("date_type") == "day_after_tomorrow":
            return today + timedelta(days=2)
        elif analysis_data.get("date_type") == "specific_date" and analysis_data.get("specific_date"):
            try:
                return datetime.strptime(analysis_data.get("specific_date"), "%Y-%m-%d")
            except:
                return today
        elif analysis_data.get("date_type") == "far_future":
            # По умолчанию через неделю
            return today + timedelta(days=7)
        else:
            return today
    except Exception as e:
        logger.error(f"Error getting date from analysis: {e}")
        return datetime.now()


def check_if_time_selection_request_ai(user_input, today_slots, tomorrow_slots):
    """
    Uses AI to determine if this is a request to select a time from previously displayed options.

    Args:
        user_input: User's input text
        today_slots: Available slots for today
        tomorrow_slots: Available slots for tomorrow

    Returns:
        bool: True if time selection request, False otherwise
    """
    try:
        # Prepare context for assistant
        context = {
            "user_input": user_input,
            "today_slots": today_slots,
            "tomorrow_slots": tomorrow_slots,
        }

        # Create prompt for assistant
        prompt = f"""
        На основе запроса пользователя: "{user_input}"
        и доступных времен:

        Сегодня: {', '.join(today_slots) if today_slots else "нет доступных времен"}
        Завтра: {', '.join(tomorrow_slots) if tomorrow_slots else "нет доступных времен"}

        Определи, выбирает ли пользователь одно из ранее предложенных времен.
        Пользователь выбирает время, если упоминает:
        - Порядковый номер ("первое время", "второй вариант", "третье")
        - Относительное положение ("самое раннее", "последнее")
        - Конкретное время из списка доступных
        - Фразы согласия после показа вариантов ("да", "хорошо", "подойдет")

        Верни только "true" или "false" без дополнительных пояснений.
        """

        from openai import OpenAI
        from django.conf import settings
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system",
                 "content": "You are an assistant that determines if a user is selecting a time from previously shown options."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=10,
            temperature=0
        )

        response_text = response.choices[0].message.content.strip().lower()
        return "true" in response_text

    except Exception as e:
        logger.error(f"Error in check_if_time_selection_request_ai: {e}")
        # Fall back to static rules
        return check_if_time_selection_request(user_input, today_slots, tomorrow_slots)


def get_selected_time_slot_ai(user_input, today_slots, tomorrow_slots):
    """
    Uses AI to determine which time slot and day the user is selecting.

    Args:
        user_input: User's input text
        today_slots: Available slots for today
        tomorrow_slots: Available slots for tomorrow

    Returns:
        tuple: (date_obj, time_str) selected by the user, or (None, None) if unable to determine
    """
    try:
        # Prepare context for assistant
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        # Create prompt for assistant
        prompt = f"""
        На основе запроса пользователя: "{user_input}"
        и доступных времен:

        Сегодня ({today.strftime('%d.%m.%Y')}): {', '.join(today_slots) if today_slots else "нет доступных времен"}
        Завтра ({tomorrow.strftime('%d.%m.%Y')}): {', '.join(tomorrow_slots) if tomorrow_slots else "нет доступных времен"}

        Определи, какое время и день выбирает пользователь.

        Если пользователь упоминает "первое", "первый вариант" и т.п. - он выбирает первое время из списка.
        Если "второе", "второй" - второе время из списка.
        Если "третье", "третий" - третье время из списка.
        Если "последнее" - последнее время из списка.

        Если пользователь прямо упоминает день (сегодня/завтра), используй слоты для этого дня.
        Если пользователь не указывает день, используй сегодняшние слоты по умолчанию.

        Если пользователь упоминает конкретное время (например, "14:30"), проверь, есть ли оно в доступных слотах.

        Верни ответ в формате JSON:
        ```json
        {
        "day": "today|tomorrow",
          "time": "HH:MM"
        }
        ```

        Если невозможно определить время, верни null вместо времени.
        """

        from openai import OpenAI
        from django.conf import settings
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system",
                 "content": "You are an assistant that determines which time slot a user is selecting from available options."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100,
            temperature=0
        )

        response_text = response.choices[0].message.content

        # Extract JSON from response
        json_match = re.search(r'```(?:json)?\s*({[\s\S]*?})\s*```', response_text)
        if json_match:
            selection = json.loads(json_match.group(1))
            day = selection.get("day")
            time = selection.get("time")

            if time:
                if day == "today":
                    return datetime.now().date(), time
                elif day == "tomorrow":
                    return (datetime.now() + timedelta(days=1)).date(), time

        # Fall back to static rules if AI selection fails
        return get_selected_time_slot(user_input, today_slots, tomorrow_slots)

    except Exception as e:
        logger.error(f"Error in get_selected_time_slot_ai: {e}")
        # Fall back to static rules
        return get_selected_time_slot(user_input, today_slots, tomorrow_slots)


def extract_date_from_request(user_input):
    """
    Extracts date from user request, handling relative date references.

    Args:
        user_input: User's input text

    Returns:
        datetime or None: Extracted date or None if not found
    """
    user_input = user_input.lower()

    # Check for today/tomorrow/day after tomorrow
    if "сегодня" in user_input:
        return datetime.now()
    elif "завтра" in user_input:
        return datetime.now() + timedelta(days=1)
    elif "послезавтра" in user_input or "после завтра" in user_input:
        return datetime.now() + timedelta(days=2)

    # Check for "через X дней/недель/месяцев"
    through_match = re.search(r'через (\d+) (дн[еяй]|недел[юьи]|месяц[еа]в?)', user_input)
    if through_match:
        amount = int(through_match.group(1))
        period = through_match.group(2)

        if period.startswith("дн"):
            return datetime.now() + timedelta(days=amount)
        elif period.startswith("недел"):
            return datetime.now() + timedelta(days=amount * 7)
        elif period.startswith("месяц"):
            return datetime.now() + timedelta(days=amount * 30)

    # Check for specific date formats (DD.MM, etc.)
    date_patterns = [
        r'(\d{1,2})[\.\/](\d{1,2})',  # DD.MM or DD/MM
        r'(\d{1,2}) (января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)'
    ]

    for pattern in date_patterns:
        date_match = re.search(pattern, user_input)
        if date_match:
            try:
                if '.' in pattern or '/' in pattern:
                    day = int(date_match.group(1))
                    month = int(date_match.group(2))
                    year = datetime.now().year
                    return datetime(year, month, day)
                else:
                    # Handle month names
                    day = int(date_match.group(1))
                    month_name = date_match.group(2)
                    month_map = {
                        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
                        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
                        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
                    }
                    month = month_map.get(month_name.lower())
                    if month:
                        year = datetime.now().year
                        return datetime(year, month, day)
            except (ValueError, TypeError):
                pass

    return None


def is_date_near_term(date_obj):
    """
    Determines if a date is near-term (today, tomorrow, or day after).

    Args:
        date_obj: Date object to check

    Returns:
        bool: True if near-term, False otherwise
    """
    if not date_obj:
        return True  # Default to near-term if no date specified

    today = datetime.now().date()

    if isinstance(date_obj, datetime):
        date_obj = date_obj.date()

    delta = (date_obj - today).days

    return 0 <= delta <= 2  # Today, tomorrow, or day after tomorrow


def has_booking_intent(user_input):
    """
    Determines if user intends to book (not just check times).

    Args:
        user_input: User's input text

    Returns:
        bool: True if booking intent detected, False otherwise
    """
    user_input = user_input.lower()
    booking_indicators = [
        "запиш", "записать", "запись",
        "забронир", "бронир",
        "назнач", "оформ",
        "перенес", "перенести",
        "регистр", "поставь", "поставьте",
        "хочу на", "нужно на"
    ]

    return any(term in user_input for term in booking_indicators)


def select_appropriate_time_from_available(user_input, available_times):
    """
    Selects the most appropriate time from available slots based on user context.

    Args:
        user_input: User's input text
        available_times: List of available time slots

    Returns:
        str: Selected time or None if no appropriate time found
    """
    if not available_times:
        return None

    user_input = user_input.lower()

    # Sort available times
    available_times.sort()

    # Check for time of day preference
    is_morning = any(term in user_input for term in ["утр", "утром", "с утра", "пораньше", "рано"])
    is_afternoon = any(term in user_input for term in ["обед", "днем", "днём", "полдень"])
    is_evening = any(term in user_input for term in ["вечер", "вечером", "ужин", "поздно", "попозже"])

    # Filter by time of day
    morning_times = [t for t in available_times if t < "12:00"]
    afternoon_times = [t for t in available_times if "12:00" <= t < "16:00"]
    evening_times = [t for t in available_times if t >= "16:00"]

    # Standard times for different parts of day
    standard_morning = "10:30"
    standard_afternoon = "13:30"
    standard_evening = "18:30"

    # Select based on preference
    if is_morning and morning_times:
        # Try to find standard morning time
        if standard_morning in morning_times:
            return standard_morning
        # Otherwise return first morning time
        return morning_times[0]

    elif is_afternoon and afternoon_times:
        # Try to find standard afternoon time
        if standard_afternoon in afternoon_times:
            return standard_afternoon
        # Otherwise return first afternoon time
        return afternoon_times[0]

    elif is_evening and evening_times:
        # Try to find standard evening time
        if standard_evening in evening_times:
            return standard_evening
        # Otherwise return first evening time
        return evening_times[0]

    # If no specific preference or no times in preferred period,
    # try to find a standard time in any period
    standard_times = [standard_morning, standard_afternoon, standard_evening]
    for time in standard_times:
        if time in available_times:
            return time

    # Default to first available time
    return available_times[0]


def extract_date_from_formatted_text(formatted_date):
    """
    Extracts a datetime object from formatted date text like "29 Января".

    Args:
        formatted_date: Formatted date string

    Returns:
        datetime: Extracted date
    """
    try:
        parts = formatted_date.split()
        if len(parts) != 2:
            return datetime.now()

        day = int(parts[0])
        month_name = parts[1]

        month_map = {
            "Января": 1, "Февраля": 2, "Марта": 3, "Апреля": 4,
            "Мая": 5, "Июня": 6, "Июля": 7, "Августа": 8,
            "Сентября": 9, "Октября": 10, "Ноября": 11, "Декабря": 12
        }

        month = month_map.get(month_name, 1)
        year = datetime.now().year

        return datetime(year, month, day)
    except:
        return datetime.now()


def determine_user_intent(user_input, context=None):
    """
    Determines user intent using NLP approach for better natural language understanding.

    Args:
        user_input: User's input text
        context: Additional context data

    Returns:
        dict: Intent type, confidence, and additional parameters
    """
    user_input = user_input.lower()

    intent = {'type': 'unknown', 'confidence': 0.0}

    # Determine date from input
    date_obj = extract_date_from_input(user_input)

    # Check for available times intent
    # Use more relevant indicators for natural language understanding
    available_indicators = [
        'свободн', 'доступн', 'окошк', 'времена', 'когда можно', 'какие',
        'запиши', 'запишите', 'когда', 'какой день', 'какое число', 'есть ли',
        'когда прийти'
    ]

    # Calculate confidence using weighted approach for better detection
    check_score = 0
    for word in available_indicators:
        if word in user_input:
            check_score += 0.2  # Increase confidence for each match
    check_score = min(check_score, 0.9)  # Cap at 0.9 to prevent overconfidence

    if check_score > 0.2:
        intent = {
            'type': 'check_available_times',
            'confidence': check_score,
            'date_obj': date_obj or datetime.now()
        }

    # Check for appointment info intent with wider range of natural language phrases
    appointment_indicators = [
        'когда у меня', 'запис', 'во сколько', 'не помню', 'напомн',
        'какая дата', 'мой прием', 'мое время', 'моя запись'
    ]

    info_score = 0
    for word in appointment_indicators:
        if word in user_input:
            info_score += 0.2
    info_score = min(info_score, 0.9)

    if info_score > check_score:
        intent = {
            'type': 'check_appointment',
            'confidence': info_score
        }

    # Check for booking intent with broader natural language understanding
    booking_indicators = [
        'запиш', 'записаться', 'перенес', 'перезапиш', 'измен', 'поставьте',
        'забронир', 'хочу попасть', 'можно записаться', 'примите меня',
        'хочу прийти', 'нужна запись', 'планирую посетить'
    ]

    booking_score = 0
    for word in booking_indicators:
        if word in user_input:
            booking_score += 0.2
    booking_score = min(booking_score, 0.9)

    if booking_score > max(check_score, info_score):
        # Extract time if present
        time_str = extract_time_from_input(user_input)

        # Check for non-working hours
        if time_str:
            try:
                hour, minute = map(int, time_str.split(':'))
                # Clinic hours: 09:00-20:30
                if hour < 9 or (hour == 20 and minute > 30) or hour > 20:
                    return {
                        'type': 'nonworking_hours',
                        'confidence': 1.0,
                        'time': time_str
                    }
            except (ValueError, TypeError):
                pass

        intent = {
            'type': 'book_specific_time' if time_str else 'book_appointment',
            'confidence': booking_score,
            'date_obj': date_obj or datetime.now(),
            'time_str': time_str or determine_time_of_day(user_input)
        }

    # Check for delete intent with broader understanding
    delete_indicators = [
        'отмен', 'удал', 'не приду', 'отказ', 'убер', 'не нужно', 'не хочу',
        'снять запись', 'передумал', 'аннулировать', 'отказываюсь', 'убрать'
    ]

    delete_score = 0
    for word in delete_indicators:
        if word in user_input:
            delete_score += 0.2
    delete_score = min(delete_score, 0.9)

    # Prevent false positives for delete when booking is mentioned
    if delete_score > max(check_score, info_score, booking_score) and not any(
            word in user_input for word in ['перенес', 'запиш']):
        intent = {
            'type': 'delete_appointment',
            'confidence': delete_score
        }

    return intent


def extract_date_from_input(user_input):
    """
    Extracts date from user input using pattern recognition.

    Args:
        user_input: User's input text

    Returns:
        datetime: Extracted date or None
    """
    user_input = user_input.lower()

    # Check for relative dates
    if 'сегодня' in user_input:
        return datetime.now()
    elif 'завтра' in user_input:
        return datetime.now() + timedelta(days=1)
    elif 'послезавтра' in user_input or 'после завтра' in user_input:
        return datetime.now() + timedelta(days=2)

    # Check for day of week
    days_of_week = {
        'понедельник': 0, 'вторник': 1, 'среду': 2, 'среда': 2,
        'четверг': 3, 'пятницу': 4, 'пятница': 4,
        'субботу': 5, 'суббота': 5, 'воскресенье': 6, 'воскресенье': 6
    }

    for day, day_num in days_of_week.items():
        if day in user_input:
            today = datetime.now()
            days_ahead = (day_num - today.weekday()) % 7
            # If today is the day and we want next week's occurrence
            if days_ahead == 0 and 'следующ' in user_input:
                days_ahead = 7
            # If day has passed this week, get next week's occurrence
            elif days_ahead == 0:
                days_ahead = 7

            return today + timedelta(days=days_ahead)

    # Check for dates in format DD.MM or similar
    date_pattern = r'(\d{1,2})[\.\/](\d{1,2})'
    date_match = re.search(date_pattern, user_input)

    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))

        # Validate date
        if 1 <= day <= 31 and 1 <= month <= 12:
            today = datetime.now()
            try:
                date_obj = datetime(today.year, month, day)

                # If date has passed, use next year
                if date_obj.date() < today.date():
                    date_obj = datetime(today.year + 1, month, day)

                return date_obj
            except ValueError:
                # Invalid date (e.g., February 30)
                return None

    return None


def extract_time_from_input(user_input):
    """
    Extracts time from user input.

    Args:
        user_input: User's input text

    Returns:
        str: Time in format HH:MM or None
    """
    user_input = user_input.lower()

    # Check for specific time in format HH:MM or H:MM
    time_pattern = r'(\d{1,2})[:\s](\d{2})'
    time_match = re.search(time_pattern, user_input)

    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))

        # Validate time
        if 0 <= hour < 24 and 0 <= minute < 60:
            # Apply rounding rules
            if 0 <= minute <= 15:
                minute = 0
            elif 16 <= minute <= 45:
                minute = 30
            elif 46 <= minute <= 59:
                minute = 0
                hour = (hour + 1) % 24

            return f"{hour:02d}:{minute:02d}"

    return None


def determine_time_of_day(user_input):
    """
    Determines appropriate time based on time of day mentions.

    Args:
        user_input: User's input text

    Returns:
        str: Suggested time in format HH:MM
    """
    user_input = user_input.lower()

    # Map time of day to specific times
    if any(word in user_input for word in ['утр', 'утром', 'с утра', 'рано']):
        return "10:30"
    elif any(word in user_input for word in ['обед', 'днем', 'полдень']):
        return "13:30"
    elif any(word in user_input for word in ['вечер', 'ужин', 'поздно', 'позже']):
        return "18:30"

    # Default to late morning
    return "10:30"


def create_enhanced_booking_instructions(user_input, context, patient_code, available_slots_context):
    """
    Creates enhanced instructions for the assistant with improved contextual
    time selection capabilities
    """
    # Basic instructions for all cases
    instructions = f"""
    # МЕДИЦИНСКИЙ АССИСТЕНТ ДЛЯ УПРАВЛЕНИЯ ЗАПИСЯМИ НА ПРИЕМ

    ## ОБЯЗАТЕЛЬНОЕ ПРАВИЛО
    ВСЕГДА вызывай одну из следующих функций в ответ на любой запрос:
    - which_time_in_certain_day
    - reserve_reception_for_patient
    - delete_reception_for_patient

    ## КОНТЕКСТ ПАЦИЕНТА
    - ID пациента: {patient_code}

    ## ДОСТУПНЫЕ СЛОТЫ
    {available_slots_context}

    ## КОНТЕКСТУАЛЬНЫЙ ВЫБОР ВРЕМЕНИ

    ### ВАЖНО: Когда пользователь ссылается на ранее показанные времена:

    1. Если пользователь использует ПОРЯДКОВЫЕ ЧИСЛИТЕЛЬНЫЕ или УКАЗАНИЯ НА ПОРЯДОК:
       - "первое время", "первый вариант", "первое", "номер один" → всегда выбирай ПЕРВОЕ время из списка
       - "второе время", "второй вариант", "второе", "номер два" → всегда выбирай ВТОРОЕ время из списка
       - "третье время", "третий вариант", "третье", "номер три" → всегда выбирай ТРЕТЬЕ время из списка

    2. Если пользователь использует ОТНОСИТЕЛЬНОЕ ПОЛОЖЕНИЕ:
       - "самое раннее", "пораньше", "раньше" → выбирай ПЕРВОЕ время из списка
       - "самое позднее", "попозже", "позже" → выбирай ПОСЛЕДНЕЕ время из списка
       - "среднее", "серединка" → выбирай время посередине списка

    3. Если пользователь упоминает КОНКРЕТНОЕ ВРЕМЯ из списка:
       - Например, "запишите на 14:30" → проверь, есть ли такое время в списке доступных слотов, и используй его
       - Если точное время отсутствует, выбери ближайшее к нему доступное время

    4. При выборе ДНЯ учитывай контекст:
       - Если пользователь явно упоминает день ("сегодня", "завтра") → используй слоты для этого дня
       - Если упоминание дня отсутствует, но говорится о выборе времени → предполагай текущий контекст разговора
       - По умолчанию, если нет указаний на день и запрос содержит формулировку выбора → используй сегодня

    5. АЛГОРИТМ решения при отсутствии явных указаний:
       - Начни с проверки сегодняшних слотов
       - Если сегодня нет доступных слотов → используй завтрашние слоты
       - Если и завтра нет слотов → сообщи, что нет доступных времен и предложи другие дни
       - Если доступных дней несколько, приоритет: сегодня > завтра > другие дни

    ## ПРИМЕРЫ ОБРАБОТКИ ЗАПРОСОВ НА ВЫБОР ВРЕМЕНИ

    1. Пример: "Давайте на первое время"
       → Выбрать первое время из списка на сегодня
       → Вызвать reserve_reception_for_patient с этим временем

    2. Пример: "Записывайте на последний вариант"
       → Выбрать последнее время из списка на сегодня
       → Вызвать reserve_reception_for_patient с этим временем

    3. Пример: "Хочу второе время завтрашнего дня"
       → Выбрать второе время из списка на завтра
       → Вызвать reserve_reception_for_patient с этим временем

    4. Пример: "Запишите на самое раннее"
       → Выбрать первое время из списка на сегодня
       → Вызвать reserve_reception_for_patient с этим временем

    ### КРИТИЧЕСКИ ВАЖНО: Всегда завершай процесс выбора времени вызовом reserve_reception_for_patient с выбранным временем и датой
    """

    # Add intent-specific instructions
    intent = determine_user_intent(user_input, context)
    intent_type = intent.get('type')

    if "первое" in user_input.lower() or "первый" in user_input.lower() or "раннее" in user_input.lower() or "номер один" in user_input.lower():
        # User is likely referring to the first time slot
        day_context = "today"
        if "завтра" in user_input.lower():
            day_context = "tomorrow"

        today = datetime.now()
        target_date = today if day_context == "today" else today + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

        # Determine the time from available slots
        target_time = None
        if day_context == "today" and context.get("today_slots") and len(context["today_slots"]) > 0:
            target_time = context["today_slots"][0]
        elif day_context == "tomorrow" and context.get("tomorrow_slots") and len(context["tomorrow_slots"]) > 0:
            target_time = context["tomorrow_slots"][0]

        if target_time:
            instructions += f"""
            ## КРИТИЧЕСКОЕ ПРАВИЛО: ВЫБРАТЬ ПЕРВОЕ ДОСТУПНОЕ ВРЕМЯ

            Пользователь запрашивает первое доступное время на {day_context}.

            ОБЯЗАТЕЛЬНО вызови:
            reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{date_str} {target_time}", trigger_id=1)

            НЕ запрашивай подтверждение, сразу выполни запись!
            """

    elif "второе" in user_input.lower() or "второй" in user_input.lower() or "номер два" in user_input.lower():
        # User is likely referring to the second time slot
        day_context = "today"
        if "завтра" in user_input.lower():
            day_context = "tomorrow"

        today = datetime.now()
        target_date = today if day_context == "today" else today + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

        # Determine the time from available slots
        target_time = None
        if day_context == "today" and context.get("today_slots") and len(context["today_slots"]) > 1:
            target_time = context["today_slots"][1]
        elif day_context == "tomorrow" and context.get("tomorrow_slots") and len(context["tomorrow_slots"]) > 1:
            target_time = context["tomorrow_slots"][1]

        if target_time:
            instructions += f"""
            ## КРИТИЧЕСКОЕ ПРАВИЛО: ВЫБРАТЬ ВТОРОЕ ДОСТУПНОЕ ВРЕМЯ

            Пользователь запрашивает второе доступное время на {day_context}.

            ОБЯЗАТЕЛЬНО вызови:
            reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{date_str} {target_time}", trigger_id=1)

            НЕ запрашивай подтверждение, сразу выполни запись!
            """

    elif "третье" in user_input.lower() or "третий" in user_input.lower() or "номер три" in user_input.lower():
        # User is likely referring to the third time slot
        day_context = "today"
        if "завтра" in user_input.lower():
            day_context = "tomorrow"

        today = datetime.now()
        target_date = today if day_context == "today" else today + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

        # Determine the time from available slots
        target_time = None
        if day_context == "today" and context.get("today_slots") and len(context["today_slots"]) > 2:
            target_time = context["today_slots"][2]
        elif day_context == "tomorrow" and context.get("tomorrow_slots") and len(context["tomorrow_slots"]) > 2:
            target_time = context["tomorrow_slots"][2]

        if target_time:
            instructions += f"""
            ## КРИТИЧЕСКОЕ ПРАВИЛО: ВЫБРАТЬ ТРЕТЬЕ ДОСТУПНОЕ ВРЕМЯ

            Пользователь запрашивает третье доступное время на {day_context}.

            ОБЯЗАТЕЛЬНО вызови:
            reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{date_str} {target_time}", trigger_id=1)

            НЕ запрашивай подтверждение, сразу выполни запись!
            """

    elif "последн" in user_input.lower() or "позднее" in user_input.lower() or "позже" in user_input.lower():
        # User is likely referring to the last time slot
        day_context = "today"
        if "завтра" in user_input.lower():
            day_context = "tomorrow"

        today = datetime.now()
        target_date = today if day_context == "today" else today + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

        # Determine the time from available slots
        target_time = None
        if day_context == "today" and context.get("today_slots") and len(context["today_slots"]) > 0:
            target_time = context["today_slots"][-1]  # Last element
        elif day_context == "tomorrow" and context.get("tomorrow_slots") and len(context["tomorrow_slots"]) > 0:
            target_time = context["tomorrow_slots"][-1]  # Last element

        if target_time:
            instructions += f"""
            ## КРИТИЧЕСКОЕ ПРАВИЛО: ВЫБРАТЬ ПОСЛЕДНЕЕ ДОСТУПНОЕ ВРЕМЯ

            Пользователь запрашивает последнее доступное время на {day_context}.

            ОБЯЗАТЕЛЬНО вызови:
            reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{date_str} {target_time}", trigger_id=1)

            НЕ запрашивай подтверждение, сразу выполни запись!
            """

    return instructions


def create_meaningful_response(user_input, patient_code, context=None):
    """
    Creates a meaningful response when the assistant fails or returns just "completed".

    Args:
        user_input: User's input text
        patient_code: Patient's code
        context: Additional context

    Returns:
        dict: Formatted response
    """
    intent = determine_user_intent(user_input, context)
    intent_type = intent.get('type')

    if intent_type == 'check_available_times':
        date_obj = intent.get('date_obj', datetime.now())
        date_str = date_obj.strftime("%Y-%m-%d")

        result = which_time_in_certain_day(patient_code, date_str)
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
        else:
            result_dict = result

        return process_which_time_response(result_dict, date_obj, patient_code)

    elif intent_type in ['book_specific_time', 'book_appointment']:
        date_obj = intent.get('date_obj', datetime.now())
        time_str = intent.get('time_str', '10:30')
        date_str = date_obj.strftime("%Y-%m-%d")
        datetime_str = f"{date_str} {time_str}"

        result = reserve_reception_for_patient(patient_code, datetime_str, 1)
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
        else:
            result_dict = result

        return process_reserve_reception_response(result_dict, date_obj, time_str)

    elif intent_type == 'delete_appointment':
        result = delete_reception_for_patient(patient_code)
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
        else:
            result_dict = result

        return process_delete_reception_response(result_dict)

    # Default response for unknown intent
    return {
        "status": "bad_user_input",
        "message": "Пожалуйста, уточните ваш запрос. Укажите конкретную дату и время для записи или запросите информацию о доступных временах."
    }


def extract_available_times(result):
    """
    Извлекает доступные времена из ответа which_time_in_certain_day в единый список.

    Args:
        result: Результат функции which_time_in_certain_day

    Returns:
        list: Список всех доступных времен в формате HH:MM
    """
    times = []

    # Проверяем все возможные поля с временами
    if "all_available_times" in result and isinstance(result["all_available_times"], list):
        times = result["all_available_times"]
    elif "suggested_times" in result and isinstance(result["suggested_times"], list):
        times = result["suggested_times"]
    else:
        # Проверяем стандартные поля
        for key in ["first_time", "second_time", "third_time"]:
            if key in result and result[key]:
                times.append(result[key])

        # Проверяем числовые поля
        for i in range(1, 10):
            key = f"time_{i}"
            if key in result and result[key]:
                times.append(result[key])

    # Очищаем форматы времени
    clean_times = []
    for t in times:
        if isinstance(t, str):
            if " " in t:  # Формат: "YYYY-MM-DD HH:MM"
                clean_times.append(t.split(" ")[1])
            else:
                # Удаляем секунды если присутствуют
                if t.count(":") == 2:  # Формат: "HH:MM:SS"
                    clean_times.append(":".join(t.split(":")[:2]))
                else:
                    clean_times.append(t)

    return clean_times


def filter_times_by_time_of_day(times, time_of_day):
    """
    Фильтрует времена по периоду дня.

    Args:
        times: Список времен в формате HH:MM
        time_of_day: Период дня ('morning', 'afternoon', 'evening')

    Returns:
        list: Отфильтрованные времена
    """
    from datetime import datetime

    filtered = []

    for time_str in times:
        try:
            time_obj = datetime.strptime(time_str, "%H:%M").time()

            if time_of_day == 'morning':
                # Утро: до 12:00
                if time_obj < datetime.strptime("12:00", "%H:%M").time():
                    filtered.append(time_str)
            elif time_of_day == 'afternoon':
                # День: 12:00 - 16:00
                if datetime.strptime("12:00", "%H:%M").time() <= time_obj < datetime.strptime("16:00", "%H:%M").time():
                    filtered.append(time_str)
            elif time_of_day == 'evening':
                # Вечер: после 16:00
                if time_obj >= datetime.strptime("16:00", "%H:%M").time():
                    filtered.append(time_str)
            else:
                # Если период не указан, включаем все
                filtered.append(time_str)
        except ValueError:
            continue

    return filtered


def get_time_of_day_from_input(user_input):
    """
    Определяет период дня из запроса пользователя.

    Args:
        user_input: Текст запроса пользователя

    Returns:
        str: Период дня ('morning', 'afternoon', 'evening', None)
    """
    user_input_lower = user_input.lower()

    # Утро
    if any(word in user_input_lower for word in ["утр", "утром", "утренн", "рано", "пораньше"]):
        return "morning"
    # День/обед
    elif any(word in user_input_lower for word in ["обед", "днем", "днём", "день", "полдень"]):
        return "afternoon"
    # Вечер
    elif any(word in user_input_lower for word in ["вечер", "вечером", "поздно", "ужин", "вечерн"]):
        return "evening"

    return None


def get_fixed_time_for_period(time_of_day):
    """
    Возвращает фиксированное время для указанного периода дня.

    Args:
        time_of_day: Период дня ('morning', 'afternoon', 'evening')

    Returns:
        str: Фиксированное время в формате HH:MM
    """
    if time_of_day == "morning":
        return "10:30"
    elif time_of_day == "afternoon":
        return "13:30"
    elif time_of_day == "evening":
        return "18:30"
    else:
        return "10:30"  # По умолчанию утро


@csrf_exempt
@require_http_methods(["GET"])
def get_assistant_info(request):
    """
    Returns information about saved assistants

    Args:
        request: HTTP request

    Returns:
        JsonResponse: Assistant information
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


def format_available_slots_for_prompt(patient, today_date, tomorrow_date):
    """
    Format available slots for assistant prompt with enhanced referencing capability
    for easier selection in follow-up requests
    """
    from reminder.models import AvailableTimeSlot

    # Get slots for today
    today_slots = AvailableTimeSlot.objects.filter(
        patient=patient,
        date=today_date
    ).order_by('time').values_list('time', flat=True)

    # Get slots for tomorrow
    tomorrow_slots = AvailableTimeSlot.objects.filter(
        patient=patient,
        date=tomorrow_date
    ).order_by('time').values_list('time', flat=True)

    # Format times
    today_times = [slot.strftime('%H:%M') for slot in today_slots]
    tomorrow_times = [slot.strftime('%H:%M') for slot in tomorrow_slots]

    # Create prompt text with explicit numbering for easier reference
    prompt_text = "\n## ДОСТУПНЫЕ ВРЕМЕННЫЕ СЛОТЫ\n\n"

    # Add today's slots with numbered reference
    prompt_text += f"### На сегодня ({today_date.strftime('%d.%m.%Y')}):\n"
    if today_times:
        for i, time in enumerate(today_times, 1):
            prompt_text += f"{i}. {time}"
            # Add descriptive labels for easier reference
            if i == 1:
                prompt_text += " (первое время сегодня)"
            elif i == 2:
                prompt_text += " (второе время сегодня)"
            elif i == 3:
                prompt_text += " (третье время сегодня)"
            prompt_text += "\n"
    else:
        prompt_text += "Нет доступных слотов\n"
    prompt_text += "\n"

    # Add tomorrow's slots with numbered reference
    prompt_text += f"### На завтра ({tomorrow_date.strftime('%d.%m.%Y')}):\n"
    if tomorrow_times:
        for i, time in enumerate(tomorrow_times, 1):
            prompt_text += f"{i}. {time}"
            # Add descriptive labels for easier reference
            if i == 1:
                prompt_text += " (первое время завтра)"
            elif i == 2:
                prompt_text += " (второе время завтра)"
            elif i == 3:
                prompt_text += " (третье время завтра)"
            prompt_text += "\n"
    else:
        prompt_text += "Нет доступных слотов\n"
    prompt_text += "\n"

    # Add reference mapping instructions for the assistant
    prompt_text += """
    ## СПРАВОЧНИК ДЛЯ ВЫБОРА ВРЕМЕНИ

    ### Прямые указания на порядковый номер:
    - "первое время" → выбрать первое (1) время из списка
    - "второе время" → выбрать второе (2) время из списка
    - "третье время" → выбрать третье (3) время из списка
    - "последнее время" → выбрать последнее время из списка

    ### Косвенные указания:
    - "самое раннее" → выбрать первое время из списка
    - "самое позднее" → выбрать последнее время из списка
    - "раньше" → выбрать первое время из списка
    - "позже" → выбрать последнее время из списка

    ### Указания с днем:
    - "на первое завтра" → выбрать первое время из списка на завтра
    - "первое время сегодня" → выбрать первое время из списка на сегодня
    - "последнее время сегодня" → выбрать последнее время из списка на сегодня

    ### Вариации на время:
    - "номер один", "первый вариант" → выбрать первое время
    - "номер два", "второй вариант" → выбрать второе время
    - "номер три", "третий вариант" → выбрать третье время
    """

    return prompt_text
