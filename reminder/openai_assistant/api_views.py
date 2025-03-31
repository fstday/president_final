import json
import logging
import calendar
from datetime import datetime, timezone, timedelta, time
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


def process_which_time_response(response_data, date_obj):
    """
    Processes and formats response from which_time_in_certain_day function.

    Args:
        response_data: Response data from which_time_in_certain_day
        date_obj: Date object for the query

    Returns:
        dict: Formatted response
    """
    try:
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

        # Extract specialist name
        specialist_name = response_data.get("specialist_name",
                                            response_data.get("doctor", "Специалист"))

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
                    "specialist_name": response_data.get("doctor_name", "Специалист"),
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
    Handles requests from the voice bot, ensuring proper response formatting.
    Modified to work with patient_code as the primary identifier.

    Accepts:
    - patient_code: Required - Unique identifier of the patient
    - user_input: Required - User's message text
    - appointment_id: Optional - Used only for operations specifically requiring an appointment ID
    """
    try:
        # Parse request data
        data = json.loads(request.body)
        patient_code = data.get('patient_code')
        user_input = data.get('user_input', '').strip()
        appointment_id = data.get('appointment_id')  # Optional, used only if needed

        logger.info(f"\n\n=================================================\n\n"
                    f"Processing request: "
                    f"patient_code={patient_code}, "
                    f"user_input='{user_input}', "
                    f"appointment_id={appointment_id}"
                    f"\n\n=================================================\n\n")

        # Validate required parameters
        if not patient_code or not user_input:
            logger.warning("Missing required parameters")
            return JsonResponse({
                'status': 'bad_user_input',
                'message': 'Missing required parameters: patient_code and user_input'
            })

        # Check if patient exists
        try:
            patient = Patient.objects.get(patient_code=patient_code)
        except Patient.DoesNotExist:
            logger.error(f"Patient with code {patient_code} not found")
            return JsonResponse({
                'status': 'error_med_element',
                'message': 'Patient not found'
            })

        # Check if patient is in the ignored list
        if IgnoredPatient.objects.filter(patient_code=patient_code).exists():
            logger.warning(f"Patient {patient_code} is in ignored list")
            return JsonResponse({
                'status': 'error_med_element',
                'message': 'Patient is in ignored list'
            })

        # Check if the patient has an active appointment (for rescheduling/checking/cancellation)
        # If appointment_id is provided, use that specific appointment instead
        appointment = None
        if appointment_id:
            try:
                appointment = Appointment.objects.get(appointment_id=appointment_id, is_active=True)
                if appointment.patient.patient_code != patient_code:
                    logger.warning(f"Appointment {appointment_id} does not belong to patient {patient_code}")
                    return JsonResponse({
                        'status': 'error_med_element',
                        'message': 'Appointment does not belong to this patient'
                    })
            except Appointment.DoesNotExist:
                logger.warning(f"Specified appointment {appointment_id} not found or not active")
                # Fall back to looking for any active appointment for this patient
                appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by(
                    '-start_time').first()
        else:
            # Look for any active appointment for this patient
            appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time').first()

        # Initialize assistant client
        assistant_client = AssistantClient()

        # Get or create a thread for dialog
        thread = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # If we have an appointment, use its ID for the thread
                # Otherwise, use the patient code directly
                thread_id = appointment.appointment_id if appointment else f"patient_{patient_code}"
                thread = assistant_client.get_or_create_thread(thread_id, patient)
                break
            except Exception as e:
                if attempt < max_attempts - 1:
                    logger.warning(f"Failed to get/create thread (attempt {attempt + 1}/{max_attempts}): {e}")
                    time.sleep(1)  # Brief delay before retry
                else:
                    logger.error(f"Failed to get/create thread after {max_attempts} attempts: {e}")
                    return JsonResponse({
                        'status': 'error_med_element',
                        'message': 'Failed to initialize conversation thread'
                    })

        if not thread:
            logger.error("Failed to create thread")
            return JsonResponse({
                'status': 'error_med_element',
                'message': 'Failed to initialize conversation thread'
            })

        # Strict instructions for forcing function calls
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

        # Attempt to add user message to thread
        try:
            assistant_client.add_message_to_thread(thread.thread_id, user_input)
        except Exception as e:
            logger.error(f"Failed to add message to thread: {e}")
            # Create fallback response based on user request
            return create_fallback_response(user_input, patient_code, appointment)

        # Run assistant with strict instructions
        try:
            run = assistant_client.run_assistant(thread, appointment if appointment else patient,
                                                 instructions=strict_instructions)
        except Exception as e:
            logger.error(f"Failed to run assistant: {e}")
            return create_fallback_response(user_input, patient_code, appointment)

        # Wait for completion and check for function calls
        try:
            result = assistant_client.wait_for_run_completion(thread.thread_id, run.run_id, timeout=40)

            # Check if we have a formatted function result
            if isinstance(result, dict) and "status" in result:
                logger.info(f"Returning function result with status: {result['status']}")
                return JsonResponse(result)
        except Exception as e:
            logger.error(f"Error waiting for run completion: {e}")
            # Continue execution and try other methods

        # Direct function call based on text request (as fallback)
        return create_fallback_response(user_input, patient_code, appointment)

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
                processed_result = process_which_time_response(result_dict, date_obj)
                return JsonResponse(processed_result)
            else:
                processed_result = process_which_time_response(result, date_obj)
                return JsonResponse(processed_result)

        # Default to checking available times for today
        else:
            date_obj = datetime.now()
            date_str = date_obj.strftime("%Y-%m-%d")

            result = which_time_in_certain_day(patient_code, date_str)

            if hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                processed_result = process_which_time_response(result_dict, date_obj)
                return JsonResponse(processed_result)
            else:
                processed_result = process_which_time_response(result, date_obj)
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


def format_available_slots_for_prompt(patient, today, tomorrow):
    """
    Форматирует доступные временные слоты для включения в промпт ассистента.

    Args:
        patient: Объект пациента
        today: Сегодняшняя дата (объект datetime.date)
        tomorrow: Завтрашняя дата (объект datetime.date)

    Returns:
        str: Отформатированная строка для включения в промпт
    """
    from reminder.models import AvailableTimeSlot

    # Получаем слоты на сегодня
    today_slots = AvailableTimeSlot.objects.filter(
        patient=patient,
        date=today
    ).order_by('time').values_list('time', flat=True)

    # Получаем слоты на завтра
    tomorrow_slots = AvailableTimeSlot.objects.filter(
        patient=patient,
        date=tomorrow
    ).order_by('time').values_list('time', flat=True)

    # Форматируем времена
    today_times = [slot.strftime('%H:%M') for slot in today_slots]
    tomorrow_times = [slot.strftime('%H:%M') for slot in tomorrow_slots]

    # Начинаем создавать текст для промпта
    prompt_text = "\n# ДОСТУПНЫЕ ВРЕМЕННЫЕ СЛОТЫ ДЛЯ ЗАПИСИ\n\n"

    # Добавляем информацию о слотах на сегодня
    prompt_text += f"## На сегодня ({today.strftime('%d.%m.%Y')}):\n"
    if today_times:
        prompt_text += ", ".join(today_times)
    else:
        prompt_text += "Нет доступных слотов"
    prompt_text += "\n\n"

    # Добавляем информацию о слотах на завтра
    prompt_text += f"## На завтра ({tomorrow.strftime('%d.%m.%Y')}):\n"
    if tomorrow_times:
        prompt_text += ", ".join(tomorrow_times)
    else:
        prompt_text += "Нет доступных слотов"
    prompt_text += "\n\n"

    # Добавляем инструкции по использованию слотов
    prompt_text += """
## ОБЯЗАТЕЛЬНЫЙ АЛГОРИТМ ЗАПИСИ БЕЗ УКАЗАНИЯ КОНКРЕТНОГО ВРЕМЕНИ:

1. Когда пациент просит "запишите меня на сегодня/завтра", но НЕ указывает конкретное время:
   - ИСПОЛЬЗУЙ доступные временные слоты из списка выше
   - ВЫБЕРИ первое доступное время из соответствующего списка
   - ВЫЗОВИ функцию reserve_reception_for_patient с этим временем
   - НЕ используй which_time_in_certain_day

2. Если на запрашиваемую дату НЕТ доступных слотов:
   - Сообщи пациенту об отсутствии свободных окон
   - Предложи другую дату, на которую есть слоты

3. Примеры правильных действий:
   Пациент: "Запишите меня на сегодня"
   Действие: reserve_reception_for_patient с первым доступным временем из списка на сегодня

   Пациент: "Запишите меня на завтра"
   Действие: reserve_reception_for_patient с первым доступным временем из списка на завтра

ВАЖНО: Этот алгоритм имеет НАИВЫСШИЙ приоритет для запросов без указания конкретного времени.
"""

    return prompt_text
