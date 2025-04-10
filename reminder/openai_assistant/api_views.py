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

from reminder.infoclinica_requests.utils import format_doctor_name
from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient, AvailableTimeSlot
from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
from reminder.openai_assistant.assistant_client import AssistantClient
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt, \
    get_time_selection_instructions, get_enhanced_comprehensive_instructions
from reminder.openai_assistant.helpers import check_if_time_selection_request, get_selected_time_slot
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


def process_which_time_response(response_data, date_obj, patient_code):
    """
    Processes and formats response from which_time_in_certain_day function.

    Args:
        response_data: Response data from which_time_in_certain_day
        date_obj: Date object for the query

    Returns:
        dict: Formatted response
        :param patient_code:
    """
    try:

        specialist_name = response_data.get("specialist_name",
                                            response_data.get("doctor", format_doctor_name(patient_code)))

        # Convert string date to datetime object if needed
        if isinstance(date_obj, str):
            try:
                if date_obj == "today":
                    date_obj = datetime.now()
                elif date_obj == "tomorrow":
                    date_obj = datetime.now() + timedelta(days=1)
                elif " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                    date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
                else:  # If date only (YYYY-MM-DD)
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
            except ValueError:
                # If can't parse, use current date
                date_obj = datetime.now()

        # Determine relation to current day
        relation = get_date_relation(date_obj)

        # Extract available times
        available_times = []

        # Check different field variants with times
        if "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
            available_times = response_data["all_available_times"]
        elif "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
            available_times = response_data["suggested_times"]
        else:
            # Check first_time, second_time, third_time fields
            for key in ["first_time", "second_time", "third_time"]:
                if key in response_data and response_data[key]:
                    available_times.append(response_data[key])

            # Check time_1, time_2, time_3... fields
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

        # Check if response indicates no available slots
        if "status" in response_data and response_data["status"].startswith("error_empty_windows"):
            return response_data

        # Format date information
        date_info = format_date_info(date_obj)

        # Determine status based on number of available times
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

        else:  # 3 or more times
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

    except Exception as e:
        logger.error(f"Error in process_which_time_response: {e}", exc_info=True)
        return {
            "status": "error_med_element",
            "message": f"Ошибка при обработке ответа о доступном времени: {str(e)}"
        }


def process_reserve_reception_response(response_data, date_obj, requested_time):
    """
    Processes and formats response from reserve_reception_for_patient function.

    Args:
        response_data: Response data from reserve_reception_for_patient
        date_obj: Date object for appointment
        requested_time: Time requested by user

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

            # Determine success status
            success_status = "success_change_reception"
            if relation == "today":
                success_status = "success_change_reception_today"
            elif relation == "tomorrow":
                success_status = "success_change_reception_tomorrow"

            # Build response
            response = {
                "status": success_status,
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

            # One alternative time
            elif len(clean_times) == 1:
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


def process_appointment_time_response(response_data):
    """
    Processes and formats response from appointment_time_for_patient function.

    Args:
        response_data: Response data from appointment_time_for_patient

    Returns:
        dict: Formatted response
    """
    try:
        # Check if response already has a proper status
        status = response_data.get("status", "")
        specialist_name = response_data.get("specialist_name", response_data.get("doctor_name", "Специалист"))

        # If successful appointment info
        if status in ["success_appointment", "success_appointment_from_db"]:
            # Validate required fields
            required_fields = ["appointment_id", "appointment_time", "appointment_date", "doctor_name"]
            for field in required_fields:
                if field not in response_data:
                    return {
                        "status": "error_med_element",
                        "message": f"В ответе отсутствует обязательное поле: {field}"
                    }

            # Parse date for proper formatting
            date_obj = None
            if "appointment_date" in response_data:
                try:
                    date_obj = datetime.strptime(response_data["appointment_date"], "%Y-%m-%d")
                except ValueError:
                    pass

            if date_obj:
                # Determine relation to current day
                relation = get_date_relation(date_obj)

                # Format date information
                date_info = format_date_info(date_obj)

                response = {
                    "status": "success_for_check_info",
                    "date": date_info["date"],
                    "date_kz": date_info["date_kz"],
                    "specialist_name": specialist_name,
                    "weekday": date_info["weekday"],
                    "weekday_kz": date_info["weekday_kz"],
                    "time": response_data["appointment_time"]
                }

                if relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            # If can't format date, return original
            return response_data

        # If error, return as is
        elif status.startswith("error_"):
            return response_data

        # Default case
        return response_data

    except Exception as e:
        logger.error(f"Error in process_appointment_time_response: {e}", exc_info=True)
        return {
            "status": "error_med_element",
            "message": f"Ошибка при обработке ответа о текущей записи: {str(e)}"
        }


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Processes requests from the voice bot with improved handling for all edge cases
    and enhanced contextual time selection.
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

        # Initialize context for the assistant
        additional_context = {}

        # Fetch available slots for today and tomorrow to provide better context
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

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

            # Check if this is a contextual time selection request
            is_time_selection = check_if_time_selection_request(user_input, today_slots, tomorrow_slots)
            if is_time_selection:
                target_date, target_time = get_selected_time_slot(user_input, today_slots, tomorrow_slots)
                if target_date and target_time:
                    # Directly book the selected time
                    date_str = target_date.strftime("%Y-%m-%d")
                    datetime_str = f"{date_str} {target_time}"

                    result = reserve_reception_for_patient(patient_code, datetime_str, 1)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_reserve_reception_response(result_dict, target_date, target_time)
                    return JsonResponse(processed_result)

        except Exception as slots_error:
            logger.error(f"Error fetching available slots: {slots_error}")
            additional_context["today_slots"] = []
            additional_context["tomorrow_slots"] = []

        # Determine intent using NLP for better handling
        intent = determine_user_intent(user_input, additional_context)

        # Handle high-confidence intents directly for reliability
        if intent.get('confidence', 0) > 0.8:
            intent_type = intent.get('type')

            # Handle available times requests
            if intent_type == 'check_available_times':
                try:
                    date_obj = intent.get('date_obj', datetime.now())
                    date_str = date_obj.strftime("%Y-%m-%d")

                    result = which_time_in_certain_day(patient_code, date_str)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_which_time_response(result_dict, date_obj, patient_code)
                    return JsonResponse(processed_result)
                except Exception as e:
                    logger.error(f"Error processing check_available_times intent: {e}")
                    fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                    return JsonResponse(fallback_response)

            # Handle appointment info requests
            elif intent_type == 'check_appointment':
                try:
                    result = appointment_time_for_patient(patient_code)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_appointment_time_response(result_dict)
                    return JsonResponse(processed_result)
                except Exception as e:
                    logger.error(f"Error processing check_appointment intent: {e}")
                    fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                    return JsonResponse(fallback_response)

            # Handle booking with specific time
            elif intent_type == 'book_specific_time':
                try:
                    date_obj = intent.get('date_obj')
                    time_str = intent.get('time_str')

                    if date_obj and time_str:
                        date_str = date_obj.strftime("%Y-%m-%d")
                        datetime_str = f"{date_str} {time_str}"

                        result = reserve_reception_for_patient(patient_code, datetime_str, 1)
                        if hasattr(result, 'content'):
                            result_dict = json.loads(result.content.decode('utf-8'))
                        else:
                            result_dict = result

                        processed_result = process_reserve_reception_response(result_dict, date_obj, time_str)
                        return JsonResponse(processed_result)
                except Exception as e:
                    logger.error(f"Error processing book_specific_time intent: {e}")
                    fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                    return JsonResponse(fallback_response)

            # Handle delete appointments
            elif intent_type == 'delete_appointment':
                try:
                    result = delete_reception_for_patient(patient_code)
                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                    else:
                        result_dict = result

                    processed_result = process_delete_reception_response(result_dict)
                    return JsonResponse(processed_result)
                except Exception as e:
                    logger.error(f"Error processing delete_appointment intent: {e}")
                    fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                    return JsonResponse(fallback_response)

        # For less certain intents or general requests, use the assistant with enhanced context
        try:
            assistant_client = AssistantClient()
            thread = assistant_client.get_or_create_thread(f"patient_{patient_code}", patient)

            # Prepare available slots context for the prompt using the enhanced formatter
            available_slots_context = format_available_slots_for_prompt(
                patient,
                datetime.now().date(),
                (datetime.now() + timedelta(days=1)).date()
            )

            # Add user message
            assistant_client.add_message_to_thread(thread.thread_id, user_input)

            # Create comprehensive instructions without using string formatting
            comprehensive_instructions = """
            # МЕДИЦИНСКИЙ АССИСТЕНТ ДЛЯ УПРАВЛЕНИЯ ЗАПИСЯМИ НА ПРИЕМ

            ## ОСНОВНАЯ ЗАДАЧА
            Ты AI-ассистент для системы управления медицинскими записями, интегрированной с Infoclinica и голосовым роботом ACS. 
            Твоя главная цель - анализировать запросы пациентов на естественном языке, определять нужное действие, 
            ВЫЗЫВАТЬ СООТВЕТСТВУЮЩУЮ ФУНКЦИЮ и форматировать ответ по требованиям системы.

            ## ДОСТУПНЫЕ ФУНКЦИИ И КОГДА ИХ ИСПОЛЬЗОВАТЬ

            1. Свободные окошки → which_time_in_certain_day(patient_code, date_time)
               Используй, когда пациент спрашивает о свободном времени на определенную дату

            2. Текущая запись → appointment_time_for_patient(patient_code)
               Используй, когда пациент интересуется своей текущей записью

            3. Запись/Перенос → reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)
               Используй, когда пациент хочет записаться на конкретное время или перенести запись

            4. Отмена записи → delete_reception_for_patient(patient_id)
               Используй, когда пациент хочет отменить запись

            ## КОНТЕКСТУАЛЬНЫЙ ВЫБОР ВРЕМЕНИ

            При выборе времени используй следующие правила:

            1. Порядковые ссылки:
               - "первое время", "первый вариант" → выбирай первое время из списка
               - "второе время", "второй вариант" → выбирай второе время из списка
               - "третье время", "третий вариант" → выбирай третье время из списка

            2. Относительные ссылки:
               - "самое раннее", "пораньше" → выбирай первое время из списка
               - "самое позднее", "попозже" → выбирай последнее время из списка

            3. Простое согласие:
               - "да", "хорошо", "ок", "согласен" → выбирай первое время из списка

            ## КРИТИЧЕСКИЕ ПРАВИЛА

            1. ВСЕГДА вызывай соответствующую функцию вместо ответа текстом
            2. При запросе на запись, ОБЯЗАТЕЛЬНО завершай процесс вызовом reserve_reception_for_patient
            3. Используй только фиксированные значения времени для периодов дня (10:30 для утра, 13:30 для дня, 18:30 для вечера)
            4. ОСОБЕННО ВАЖНО: Если запрос похож на выбор из ранее показанных времен (например, "первое время", "второй вариант"), 
               обязательно выбери соответствующее время из доступных слотов и вызови reserve_reception_for_patient
               
            "Короткие или неполные запросы (например, 'перенесите') — то отдавай статус - 'status': 'bad_user_input'."
            "Если пользовательский ввод слишком короткий, обрывочный или непонятный (например, 'перенесите', 'перенесите на', 'перезапишите', 'перезапишите на', 'какие есть', 'свободные времена на', 'какие есть свободные', 'какие', 'что доступно сейчас', 'что доступно в', 'что доступно в', 'что доступно на', 'что можно выбрать', 'что можно записать', 'что можно', 'что доступно', 'что свободно', 'можно ли время в', 'можно ли время на', 'можно ли время на', 'можно ли время', 'возможно ли время на', 'возможно ли записаться', 'возможно ли записаться на', 'возможно ли записаться в', 'возможно ли в', 'в какой день можно', 'в какое время можно', 'на когда можно', 'на когда доступно', 'какое время есть', 'доступное время', 'свободное время', 'выбрать дату', 'выбрать день', 'выбрать время', 'перезаписать', 'перенести запись', 'перезаписать', 'подвинуть', 'подвинуть на', 'подвинуть запись', 'подвинуть запись на', 'сместить', 'сместить запись', 'сместить запись на', 'сместить запись на время', 'отодвинуть', 'отодвинуть на', 'отодвинуть запись', 'отодвинуть запись на', 'отодвинуть запись на время', 'сдвинуть', 'сдвинуть на', 'сдвинуть запись', 'сдвинуть запись на', 'сдвинуть запись на время', 'переместить', 'переместить на', 'переместить запись', 'переместить запись на время', 'поменять', 'поменять на', 'поменять запись', 'поменять запись на время', 'переставить', 'переставить на', 'переставить запись', 'переставить запись на время', 'переоформить', 'переоформить на', 'переоформить запись', 'переоформить запись на время', 'изменить', 'изменить запись', 'изменить на', 'изменить запись на время', 'переписать', 'переписать на', 'переписать запись', 'перезаписать запись на время', 'назначить заново', 'назначить заново на', 'назначить заново на время', 'назначить заново запись на время', 'пересмотреть', 'пересмотреть на', 'пересмотреть на', 'пересмотреть время', 'пересмотреть время на', 'пересмотреть время записи на', 'пересогласовать', 'пересогласовать на', 'пересогласовать время', 'пересогласовать запись', 'пересогласовать время записи на', 'переместить', 'переместить на', 'переместить запись', 'переместить запись', 'переместить время записи на', 'сместить', 'сместить на', 'сместить запись', 'сместить запись на', 'сместить время записи на', 'сдвинуть', 'сдвинуть на', 'сдвинуть запись', 'сдвинуть запись на', 'сдвинуть время записи на', 'отложить', 'отложить на', 'отложить запись', 'отложить запись на', 'отложить время записи на', 'подвинуть', 'подвинуть на', 'подвинуть запись', 'подвинуть запись на', 'подвинуть время записи на', 'отложить', 'отложить на', 'отложить запись', 'отложить запись на', 'отложить время записи на', 'назначить', 'назначить на', 'назначить запись', 'назначить запись на', 'назначить время записи на'), возвращай action='bad_user_input'. "
    
            "**Исключение**:"
            "Фразы, содержащие слова 'раньше' или 'позже' (например, 'перенесите раньше', 'отодвиньте позже', 'подвиньте на раньше', 'сместите запись на позже') **не должны восприниматься как `bad_user_input`**. Вместо этого:"
            "- Если фраза содержит 'раньше' — выбери ближайшее доступное время **до текущей записи**."
            "- Если фраза содержит 'позже' — выбери ближайшее доступное время **после текущей записи**."
            
            -  Рабочие часы клиники: с 09:00 до 20:30. Если пациент предлагает записать на время, вне рабочих часов клиники - отдавай - "status": "nonworktime"
            """

            # Add patient-specific and request-specific context
            patient_specific_instructions = f"""
            # ТЕКУЩИЙ КОНТЕКСТ

            - ID пациента: {patient_code}
            - Текущий запрос: "{user_input}"

            # ДОСТУПНЫЕ ВРЕМЕННЫЕ СЛОТЫ

            {available_slots_context}
            """

            # Combine instructions without string formatting
            enhanced_comprehensive_instructions = get_enhanced_comprehensive_instructions(
                user_input=user_input,
                patient_code=patient_code,
                thread_id=thread.thread_id,
                assistant_client=assistant_client
            )

            final_instructions = (comprehensive_instructions +
                                  patient_specific_instructions +
                                  enhanced_comprehensive_instructions
                                  )

            # Run assistant with instructions
            run = assistant_client.run_assistant(thread, patient, instructions=final_instructions)

            # Wait for response with timeout
            result = assistant_client.wait_for_run_completion(thread.thread_id, run.run_id, timeout=15)

            # Validate the result to ensure it has a valid status
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

            # Analyze result and ensure it's properly formatted
            if isinstance(result, dict) and "status" in result:
                if result["status"] in valid_statuses:
                    return JsonResponse(result)
                else:
                    # If status is not valid, use fallback
                    logger.warning(f"Invalid status received from assistant: {result.get('status')}")
                    fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
                    return JsonResponse(fallback_response)

            # If no proper result, create a meaningful fallback response
            logger.warning(f"Assistant did not return a valid response for user input: {user_input}")
            fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
            return JsonResponse(fallback_response)

        except Exception as assistant_error:
            logger.error(f"Error using assistant: {assistant_error}", exc_info=True)
            fallback_response = create_meaningful_response(user_input, patient_code, additional_context)
            return JsonResponse(fallback_response)

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JsonResponse({'status': 'bad_user_input'})


def determine_user_intent(user_input, context=None):
    """
    Determines user intent using NLP-inspired approach instead of static phrase checking.

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
    available_times_indicators = ['свободн', 'доступн', 'окошк', 'времена', 'когда можно', 'какие']
    check_score = sum(2 if word in user_input else 0 for word in available_times_indicators) / len(
        available_times_indicators)

    if check_score > 0.2:
        intent = {
            'type': 'check_available_times',
            'confidence': check_score,
            'date_obj': date_obj or datetime.now()
        }

    # Check for appointment info intent
    appointment_indicators = ['когда', 'запис', 'во сколько', 'не помню', 'напомн', 'какая дата']
    info_score = sum(2 if word in user_input else 0 for word in appointment_indicators) / len(appointment_indicators)

    if info_score > check_score:
        intent = {
            'type': 'check_appointment',
            'confidence': info_score
        }

    # Check for booking intent
    booking_indicators = ['запиш', 'записаться', 'перенес', 'перезапиш', 'измен', 'поставьте']
    booking_score = sum(2 if word in user_input else 0 for word in booking_indicators) / len(booking_indicators)

    if booking_score > max(check_score, info_score):
        # Extract time if present
        time_str = extract_time_from_input(user_input)

        intent = {
            'type': 'book_specific_time' if time_str else 'book_appointment',
            'confidence': booking_score,
            'date_obj': date_obj or datetime.now(),
            'time_str': time_str or determine_time_of_day(user_input)
        }

    # Check for delete intent
    delete_indicators = ['отмен', 'удал', 'не приду', 'отказ', 'убер', 'не нужно', 'не хочу']
    delete_score = sum(2 if word in user_input else 0 for word in delete_indicators) / len(delete_indicators)

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
    - appointment_time_for_patient
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

    elif intent_type == 'check_appointment':
        result = appointment_time_for_patient(patient_code)
        if hasattr(result, 'content'):
            result_dict = json.loads(result.content.decode('utf-8'))
        else:
            result_dict = result

        return process_appointment_time_response(result_dict)

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
    """Extract available times from which_time_in_certain_day result"""
    times = []

    # Check all possible time fields
    if "all_available_times" in result and isinstance(result["all_available_times"], list):
        times = result["all_available_times"]
    elif "suggested_times" in result and isinstance(result["suggested_times"], list):
        times = result["suggested_times"]
    else:
        # Check standard fields
        for key in ["first_time", "second_time", "third_time"]:
            if key in result and result[key]:
                times.append(result[key])

        # Check numeric fields
        for i in range(1, 10):
            key = f"time_{i}"
            if key in result and result[key]:
                times.append(result[key])

    # Clean up time format
    clean_times = []
    for t in times:
        if isinstance(t, str):
            if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                clean_times.append(t.split(" ")[1])
            else:
                # Remove seconds if present
                if t.count(":") == 2:  # Format: "HH:MM:SS"
                    clean_times.append(":".join(t.split(":")[:2]))
                else:
                    clean_times.append(t)

    return clean_times


def create_fallback_response(user_input, patient_code):
    """
    Creates a fallback response when the main processing flow fails.
    Makes direct function calls based on user input to ensure a meaningful response.

    Args:
        user_input: The user's input text
        patient_code: The patient's ID code

    Returns:
        JsonResponse: A properly formatted response
    """
    try:
        user_input = user_input.lower()

        # Check for appointment info request (highest priority for clarity)
        if any(word in user_input for word in ["когда", "какая", "время", "записан", "во сколько", "не помню"]):
            result = appointment_time_for_patient(patient_code)

            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                processed_result = process_appointment_time_response(result_dict)
                return JsonResponse(processed_result)
            else:
                processed_result = process_appointment_time_response(result)
                return JsonResponse(processed_result)

        # Check for cancel request
        elif any(word in user_input for word in ["отмен", "удал", "не приду", "убер"]):
            if not any(word in user_input for word in ["перенес", "перезапиш", "измен"]):
                result = delete_reception_for_patient(patient_code)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                    processed_result = process_delete_reception_response(result_dict)
                    return JsonResponse(processed_result)
                else:
                    processed_result = process_delete_reception_response(result)
                    return JsonResponse(processed_result)

        # Check for booking/rescheduling request
        elif any(word in user_input for word in ["запиш", "записаться", "перенес", "перезапиш", "измен"]):
            # Determine date from request
            date_obj = datetime.now()
            time_str = "10:30"  # Default morning time

            if "завтра" in user_input:
                date_obj = datetime.now() + timedelta(days=1)
            elif "послезавтра" in user_input or "после завтра" in user_input:
                date_obj = datetime.now() + timedelta(days=2)

            # Determine time from request
            if "утр" in user_input or "утром" in user_input or "с утра" in user_input:
                time_str = "10:30"
            elif "обед" in user_input or "днем" in user_input or "в обед" in user_input:
                time_str = "13:30"
            elif "вечер" in user_input or "вечером" in user_input or "ужин" in user_input:
                time_str = "18:30"

            # Extract explicit time if present (HH:MM format)
            import re
            time_match = re.search(r'(\d{1,2}):(\d{2})', user_input)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                if 0 <= hour < 24 and 0 <= minute < 60:
                    time_str = f"{hour:02d}:{minute:02d}"

            # Format date and time
            date_str = date_obj.strftime("%Y-%m-%d")
            datetime_str = f"{date_str} {time_str}"

            # Call function
            result = reserve_reception_for_patient(patient_code, datetime_str, 1)

            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                processed_result = process_reserve_reception_response(result_dict, date_obj, time_str)
                return JsonResponse(processed_result)
            else:
                processed_result = process_reserve_reception_response(result, date_obj, time_str)
                return JsonResponse(processed_result)

        # Check for available time request
        elif any(word in user_input for word in ["свобод", "доступн", "окошк", "када", "когда можно"]):
            # Determine date from request
            if "завтра" in user_input:
                date_obj = datetime.now() + timedelta(days=1)
            elif "послезавтра" in user_input or "после завтра" in user_input:
                date_obj = datetime.now() + timedelta(days=2)
            else:
                date_obj = datetime.now()  # Default to today

            date_str = date_obj.strftime("%Y-%m-%d")

            # Call function
            result = which_time_in_certain_day(patient_code, date_str)

            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                processed_result = process_which_time_response(result_dict, date_obj, patient_code)
                return JsonResponse(processed_result)
            else:
                processed_result = process_which_time_response(result, date_obj, patient_code)
                return JsonResponse(processed_result)

        # Default to checking available times for today
        else:
            date_obj = datetime.now()
            date_str = date_obj.strftime("%Y-%m-%d")

            result = which_time_in_certain_day(patient_code, date_str)

            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                processed_result = process_which_time_response(result_dict, date_obj, patient_code)
                return JsonResponse(processed_result)
            else:
                processed_result = process_which_time_response(result, date_obj, patient_code)
                return JsonResponse(processed_result)

    except Exception as e:
        logger.error(f"Error in fallback response generation: {e}", exc_info=True)
        return JsonResponse({
            "status": "error_med_element",
            "message": "Произошла ошибка при обработке запроса"
        })


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
