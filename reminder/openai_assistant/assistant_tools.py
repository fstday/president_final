import json
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient

logger = logging.getLogger(__name__)


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

# Updated tools definitions with clearer descriptions and examples
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delete_reception_for_patient",
            "description": "Use this function to delete (cancel) a patient's appointment",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "Patient identifier code (patient_code)"
                    }
                },
                "required": ["patient_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reserve_reception_for_patient",
            "description": "Use this function to create a new appointment or reschedule an existing one. Use trigger_id=2 to find available times near the requested time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "Patient identifier code (patient_code)"
                    },
                    "date_from_patient": {
                        "type": "string",
                        "description": "Appointment date and time in YYYY-MM-DD HH:MM format"
                    },
                    "trigger_id": {
                        "type": "integer",
                        "description": "1=standard booking, 2=check available times near requested time, 3=check availability for day",
                        "enum": [1, 2, 3],
                        "default": 1
                    }
                },
                "required": ["patient_id", "date_from_patient"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "appointment_time_for_patient",
            "description": "Use this function to get information about a patient's current appointment",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_code": {
                        "type": "string",
                        "description": "Patient identifier code (patient_code)"
                    },
                    "year_from_patient_for_returning": {
                        "type": "string",
                        "description": "Optional date and time in YYYY-MM-DD HH:MM format to use for formatting"
                    }
                },
                "required": ["patient_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "which_time_in_certain_day",
            "description": "ALWAYS use this function when the user asks about available slots. It gets available appointment times for a specific day.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_code": {
                        "type": "string",
                        "description": "Patient identifier code (patient_code)"
                    },
                    "date_time": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format to check for available times, or 'today' for current day"
                    }
                },
                "required": ["patient_code", "date_time"]
            }
        }
    }
]


# Function to create or update assistant with tools
def create_assistant_with_tools(client, name: str, instructions: str, model: str = "gpt-4"):
    """
    Creates or updates an assistant with configured tools

    Args:
        client: OpenAI client
        name: Assistant name
        instructions: Instructions for the assistant
        model: AI model to use

    Returns:
        dict: Created or updated assistant
    """
    try:
        # Get list of existing assistants
        assistants = client.beta.assistants.list(limit=100)
        existing_assistant = None

        # Check if assistant with this name exists
        for assistant in assistants.data:
            if assistant.name == name:
                existing_assistant = assistant
                break

        if existing_assistant:
            # Update existing assistant
            updated_assistant = client.beta.assistants.update(
                assistant_id=existing_assistant.id,
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return updated_assistant
        else:
            # Create new assistant
            new_assistant = client.beta.assistants.create(
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return new_assistant

    except Exception as e:
        raise Exception(f"Error creating/updating assistant: {str(e)}")


# Response templates for various statuses
# Updated response templates that match the documentation
RESPONSE_TEMPLATES = {
    # Success responses for appointment rescheduling
    "success_change_reception": {
        "status": "success_change_reception",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "time": "{time}"
    },
    "success_change_reception_today": {
        "status": "success_change_reception_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "time": "{time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "success_change_reception_tomorrow": {
        "status": "success_change_reception_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "Бейсенбі",
        "time": "{time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Error responses for appointment changes with alternatives
    "error_change_reception": {
        "status": "error_change_reception",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}"
    },
    "error_change_reception_today": {
        "status": "error_change_reception_today",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "error_change_reception_tomorrow": {
        "status": "error_change_reception_tomorrow",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },
    "error_change_reception_bad_date": {
        "status": "error_change_reception_bad_date",
        "data": "{message}"
    },

    # Available time query statuses
    "which_time": {
        "status": "which_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}"
    },
    "which_time_today": {
        "status": "which_time_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "which_time_tomorrow": {
        "status": "which_time_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "third_time": "{third_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Error responses for empty slots
    "error_empty_windows": {
        "status": "error_empty_windows",
        "message": "Свободных приемов не найдено."
    },
    "error_empty_windows_today": {
        "status": "error_empty_windows_today",
        "message": "Свободных приемов на сегодня не найдено.",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "error_empty_windows_tomorrow": {
        "status": "error_empty_windows_tomorrow",
        "message": "Свободных приемов на завтра не найдено.",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Limited options statuses - Only one time available
    "only_first_time": {
        "status": "only_first_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}"
    },
    "only_first_time_today": {
        "status": "only_first_time_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "only_first_time_tomorrow": {
        "status": "only_first_time_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Limited options statuses - Only two times available
    "only_two_time": {
        "status": "only_two_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}"
    },
    "only_two_time_today": {
        "status": "only_two_time_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "only_two_time_tomorrow": {
        "status": "only_two_time_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Change with limited options - Only one alternative
    "change_only_first_time": {
        "status": "change_only_first_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}"
    },
    "change_only_first_time_today": {
        "status": "change_only_first_time_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "change_only_first_time_tomorrow": {
        "status": "change_only_first_time_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Change with limited options - Only two alternatives
    "change_only_two_time": {
        "status": "change_only_two_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}"
    },
    "change_only_two_time_today": {
        "status": "change_only_two_time_today",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "day": "сегодня",
        "day_kz": "бүгін"
    },
    "change_only_two_time_tomorrow": {
        "status": "change_only_two_time_tomorrow",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
        "second_time": "{second_time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Appointment deletion statuses
    "success_deleting_reception": {
        "status": "success_deleting_reception",
        "message": "Запись успешно удалена"
    },
    "error_deleting_reception": {
        "status": "error_deleting_reception",
        "message": "{message}"
    },

    # Miscellaneous status codes
    "nonworktime": {
        "status": "nonworktime"
    },
    "bad_user_input": {
        "status": "bad_user_input"
    },
    "error_med_element": {
        "status": "error_med_element",
        "message": "{message}"
    },
    "no_action_required": {
        "status": "no_action_required",
        "message": "Для данного запроса не требуется выполнение функций"
    }
}


def format_response(status_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats response according to the required format from documentation.
    Enhanced to correctly handle different status types and ensure they match
    the expected format.

    Args:
        status_type: Response status type
        data: Data to include in response

    Returns:
        dict: Formatted response
    """
    # Determine status for today/tomorrow dynamically if not already specified
    if "_today" not in status_type and "_tomorrow" not in status_type:
        try:
            date_fields = []
            # Find all date fields
            for field in ["date", "appointment_date", "date_time", "date_from_patient"]:
                if field in data and data[field]:
                    date_fields.append(data[field])

            if date_fields:
                # Take the first found date
                date_str = date_fields[0]

                # If date is in format "29 Января" - don't try to parse
                if not any(c.isdigit() for c in date_str):
                    pass
                else:
                    # Extract only the date part if there's both date and time
                    if " " in date_str and re.match(r"\d{4}-\d{2}-\d{2}", date_str.split(" ")[0]):
                        date_str = date_str.split(" ")[0]

                    # Try various date formats
                    date_formats = ["%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"]

                    # Try different formats
                    date_obj = None
                    for fmt in date_formats:
                        try:
                            date_obj = datetime.strptime(date_str, fmt).date()
                            break
                        except ValueError:
                            continue

                    # If we successfully parsed the date
                    if date_obj:
                        today = datetime.now().date()
                        tomorrow = today + timedelta(days=1)

                        # Add day information to status
                        if date_obj == today:
                            if status_type == "success_change_reception":
                                status_type = "success_change_reception_today"
                            elif status_type == "which_time":
                                status_type = "which_time_today"
                            elif status_type == "error_empty_windows":
                                status_type = "error_empty_windows_today"
                            elif status_type == "only_first_time":
                                status_type = "only_first_time_today"
                            elif status_type == "only_two_time":
                                status_type = "only_two_time_today"
                            elif status_type == "error_change_reception":
                                status_type = "error_change_reception_today"
                            elif status_type == "change_only_first_time":
                                status_type = "change_only_first_time_today"
                            elif status_type == "change_only_two_time":
                                status_type = "change_only_two_time_today"
                        elif date_obj == tomorrow:
                            if status_type == "success_change_reception":
                                status_type = "success_change_reception_tomorrow"
                            elif status_type == "which_time":
                                status_type = "which_time_tomorrow"
                            elif status_type == "error_empty_windows":
                                status_type = "error_empty_windows_tomorrow"
                            elif status_type == "only_first_time":
                                status_type = "only_first_time_tomorrow"
                            elif status_type == "only_two_time":
                                status_type = "only_two_time_tomorrow"
                            elif status_type == "error_change_reception":
                                status_type = "error_change_reception_tomorrow"
                            elif status_type == "change_only_first_time":
                                status_type = "change_only_first_time_tomorrow"
                            elif status_type == "change_only_two_time":
                                status_type = "change_only_two_time_tomorrow"
        except Exception as e:
            logger.error(f"Error determining today/tomorrow status: {e}")

    # Check number of available times
    if status_type.startswith("which_time") and not status_type.startswith("which_time_in_certain_day"):
        # Check how many times are available
        time_fields = ["time_1", "time_2", "time_3", "first_time", "second_time", "third_time"]
        times = []

        for field in time_fields:
            if field in data and data[field]:
                times.append(data[field])

        # Check all_available_times, suggested_times if standard fields are empty
        if not times:
            if "all_available_times" in data and isinstance(data["all_available_times"], list):
                times = data["all_available_times"]
            elif "suggested_times" in data and isinstance(data["suggested_times"], list):
                times = data["suggested_times"]

        # Determine correct status based on number of available times
        if not times:
            if "today" in status_type:
                status_type = "error_empty_windows_today"
            elif "tomorrow" in status_type:
                status_type = "error_empty_windows_tomorrow"
            else:
                status_type = "error_empty_windows"
        elif len(times) == 1:
            if "today" in status_type:
                status_type = "only_first_time_today"
            elif "tomorrow" in status_type:
                status_type = "only_first_time_tomorrow"
            else:
                status_type = "only_first_time"
        elif len(times) == 2:
            if "today" in status_type:
                status_type = "only_two_time_today"
            elif "tomorrow" in status_type:
                status_type = "only_two_time_tomorrow"
            else:
                status_type = "only_two_time"

    # Similarly for error_change_reception
    if status_type.startswith("error_change_reception") and not status_type.endswith("_bad_date"):
        # Check how many times are available
        time_fields = ["time_1", "time_2", "time_3", "first_time", "second_time", "third_time"]
        times = []

        for field in time_fields:
            if field in data and data[field]:
                times.append(data[field])

        # Check all_available_times, suggested_times if standard fields are empty
        if not times:
            if "all_available_times" in data and isinstance(data["all_available_times"], list):
                times = data["all_available_times"]
            elif "suggested_times" in data and isinstance(data["suggested_times"], list):
                times = data["suggested_times"]

        # Determine correct status based on number of available times
        if len(times) == 1:
            if "today" in status_type:
                status_type = "change_only_first_time_today"
            elif "tomorrow" in status_type:
                status_type = "change_only_first_time_tomorrow"
            else:
                status_type = "change_only_first_time"
        elif len(times) == 2:
            if "today" in status_type:
                status_type = "change_only_two_time_today"
            elif "tomorrow" in status_type:
                status_type = "change_only_two_time_tomorrow"
            else:
                status_type = "change_only_two_time"

    # Format date information if needed
    date_obj = None
    if "date_obj" in data or "date_time" in data:
        try:
            date_field = data.get("date_obj") or data.get("date_time")
            if isinstance(date_field, str):
                if " " in date_field:  # If date with time (YYYY-MM-DD HH:MM)
                    date_obj = datetime.strptime(date_field.split(" ")[0], "%Y-%m-%d")
                else:  # If date only (YYYY-MM-DD)
                    date_obj = datetime.strptime(date_field, "%Y-%m-%d")
            elif hasattr(date_field, 'date'):  # If it's a datetime object
                date_obj = date_field

            if date_obj:
                day = date_obj.day
                month_num = date_obj.month
                weekday = date_obj.weekday()

                # Add date fields if not present
                if "date" not in data:
                    data["date"] = f"{day} {MONTHS_RU[month_num]}"
                if "date_kz" not in data:
                    data["date_kz"] = f"{day} {MONTHS_KZ[month_num]}"
                if "weekday" not in data:
                    data["weekday"] = WEEKDAYS_RU[weekday]
                if "weekday_kz" not in data:
                    data["weekday_kz"] = WEEKDAYS_KZ[weekday]
        except Exception as e:
            logger.warning(f"Error formatting date information: {e}")

    # Get template for specified status
    if status_type in RESPONSE_TEMPLATES:
        template = RESPONSE_TEMPLATES[status_type].copy()

        # Fill template with data
        for key, value in template.items():
            if isinstance(value, str) and "{" in value and "}" in value:
                field_name = value.strip("{}")
                if field_name in data:
                    template[key] = data[field_name]

        # Normalize time format
        for key in ["time", "first_time", "second_time", "third_time"]:
            if key in template and isinstance(template[key], str):
                # If time is in format "YYYY-MM-DD HH:MM:SS"
                if " " in template[key] and len(template[key]) > 10:
                    template[key] = template[key].split(" ")[1]  # Take only time part

                # If time contains seconds (HH:MM:SS), remove them
                if template[key].count(":") == 2:
                    template[key] = ":".join(template[key].split(":")[:2])

        # Handle special cases for status types that require specific fields
        if status_type.startswith("only_first_time"):
            if "first_time" not in template or not template["first_time"]:
                template["first_time"] = data.get("time") or data.get("time_1") or data.get("suggested_times", [""])[0]

        elif status_type.startswith("only_two_time"):
            if "first_time" not in template or not template["first_time"]:
                template["first_time"] = data.get("time_1") or data.get("suggested_times", [""])[0]
            if "second_time" not in template or not template["second_time"]:
                template["second_time"] = data.get("time_2") or data.get("suggested_times", ["", ""])[1]

        elif status_type.startswith("which_time") or status_type.startswith("error_change_reception"):
            if "first_time" not in template or not template["first_time"]:
                template["first_time"] = data.get("time_1") or data.get("suggested_times", [""])[0]
            if "second_time" not in template or not template["second_time"]:
                template["second_time"] = data.get("time_2") or data.get("suggested_times", ["", ""])[1]
            if "third_time" not in template or not template["third_time"]:
                template["third_time"] = data.get("time_3") or data.get("suggested_times", ["", "", ""])[2] if len(
                    data.get("suggested_times", [])) > 2 else ""

        # Add additional fields not in template
        for key, value in data.items():
            if key not in template and key != "status" and not key.startswith("date_obj"):
                template[key] = value

        return template

    # If no matching template, return original data but add correct status
    if 'status' not in data:
        data['status'] = status_type
    return data


def get_date_relation(date_obj):
    """
    Determines relation of date to current day (today/tomorrow/other)

    Args:
        date_obj: Date object

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


def format_available_times_response(times, date_obj, specialist_name, relation=None):
    """
    Formats response with available times according to the required format

    Args:
        times: List of available times
        date_obj: Date object
        specialist_name: Name of specialist
        relation: Relation to current day ('today', 'tomorrow', or None)

    Returns:
        dict: Formatted response
    """
    # Format date information
    if isinstance(date_obj, str):
        try:
            if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
            else:  # If date only (YYYY-MM-DD)
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
        except ValueError:
            pass

    # Format day, month and weekday
    if hasattr(date_obj, 'day'):
        day = date_obj.day
        month_num = date_obj.month
        weekday = date_obj.weekday()

        date_ru = f"{day} {MONTHS_RU[month_num]}"
        date_kz = f"{day} {MONTHS_KZ[month_num]}"
        weekday_ru = WEEKDAYS_RU[weekday]
        weekday_kz = WEEKDAYS_KZ[weekday]
    else:
        # Default values if date_obj is not a valid date
        date_ru = "Неизвестно"
        date_kz = "Белгісіз"
        weekday_ru = "Неизвестно"
        weekday_kz = "Белгісіз"

    # Determine base status based on number of times
    if not times:
        base_status = "error_empty_windows"
    elif len(times) == 1:
        base_status = "only_first_time"
    elif len(times) == 2:
        base_status = "only_two_time"
    else:
        base_status = "which_time"

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
        "date": date_ru,
        "date_kz": date_kz,
        "specialist_name": specialist_name,
        "weekday": weekday_ru,
        "weekday_kz": weekday_kz
    }

    # Add day information if today or tomorrow
    if relation == "today":
        response["day"] = "сегодня"
        response["day_kz"] = "бүгін"
    elif relation == "tomorrow":
        response["day"] = "завтра"
        response["day_kz"] = "ертең"

    # Add available times based on their count
    if times:
        if len(times) >= 1:
            response["first_time"] = times[0]
        if len(times) >= 2:
            response["second_time"] = times[1]
        if len(times) >= 3:
            response["third_time"] = times[2]

    # Add message if no times available
    if not times:
        if relation == "today":
            response["message"] = "Свободных приемов на сегодня не найдено."
        elif relation == "tomorrow":
            response["message"] = "Свободных приемов на завтра не найдено."
        else:
            response["message"] = f"Свободных приемов на {date_ru} не найдено."

    return response


def process_which_time_response(response_data, date_obj):
    """
    Processes and transforms response from which_time_in_certain_day function
    with enhanced handling of various response formats.

    Args:
        response_data: Response data from which_time_in_certain_day
        date_obj: Date object for the query

    Returns:
        dict: Formatted response
    """
    # Determine relation of date to current day
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

    # Extract only time from "YYYY-MM-DD HH:MM" format
    available_times = [t.split(" ")[1] if " " in t else t for t in available_times]

    # Remove seconds if present
    available_times = [":".join(t.split(":")[:2]) if t.count(":") == 2 else t for t in available_times]

    # Extract specialist name
    specialist_name = response_data.get("specialist_name", "Специалист")

    # Check if response_data indicates no available slots
    if "status" in response_data and response_data["status"].startswith("error_empty_windows"):
        # Return no slots message directly
        return response_data

    # Format response
    return format_available_times_response(available_times, date_obj, specialist_name, relation)


def format_error_scheduling_response(times, date_obj, specialist_name, relation=None):
    """
    Formats error response with alternative times

    Args:
        times: List of alternative times
        date_obj: Date object
        specialist_name: Name of specialist
        relation: Relation to current day ('today', 'tomorrow', or None)

    Returns:
        dict: Formatted response
    """
    # Format date information
    if isinstance(date_obj, str):
        try:
            if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
            else:  # If date only (YYYY-MM-DD)
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
        except ValueError:
            pass

    # Format day, month and weekday
    if hasattr(date_obj, 'day'):
        day = date_obj.day
        month_num = date_obj.month
        weekday = date_obj.weekday()

        date_ru = f"{day} {MONTHS_RU[month_num]}"
        date_kz = f"{day} {MONTHS_KZ[month_num]}"
        weekday_ru = WEEKDAYS_RU[weekday]
        weekday_kz = WEEKDAYS_KZ[weekday]
    else:
        # Default values if date_obj is not a valid date
        date_ru = "Неизвестно"
        date_kz = "Белгісіз"
        weekday_ru = "Неизвестно"
        weekday_kz = "Белгісіз"

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
        "date": date_ru,
        "date_kz": date_kz,
        "specialist_name": specialist_name,
        "weekday": weekday_ru,
        "weekday_kz": weekday_kz
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


def format_success_scheduling_response(time, date_obj, specialist_name, relation=None):
    """
    Formats response for successful scheduling with proper date information

    Args:
        time: Appointment time
        date_obj: Date object
        specialist_name: Name of specialist
        relation: Relation to current day ('today', 'tomorrow', or None)

    Returns:
        dict: Formatted response
    """
    # Format date information
    if isinstance(date_obj, str):
        try:
            if " " in date_obj:  # If date with time (YYYY-MM-DD HH:MM)
                date_obj = datetime.strptime(date_obj.split(" ")[0], "%Y-%m-%d")
            else:  # If date only (YYYY-MM-DD)
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
        except ValueError:
            pass

    # Determine relation to today/tomorrow based on date_obj, not passed relation
    if hasattr(date_obj, 'date'):
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        date_only = date_obj.date() if hasattr(date_obj, 'date') else date_obj

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

    # Format day, month and weekday
    if hasattr(date_obj, 'day'):
        day = date_obj.day
        month_num = date_obj.month
        weekday = date_obj.weekday()

        date_ru = f"{day} {MONTHS_RU[month_num]}"
        date_kz = f"{day} {MONTHS_KZ[month_num]}"
        weekday_ru = WEEKDAYS_RU[weekday]
        weekday_kz = WEEKDAYS_KZ[weekday]
    else:
        # Default values if date_obj is not a valid date
        date_ru = "Неизвестно"
        date_kz = "Белгісіз"
        weekday_ru = "Неизвестно"
        weekday_kz = "Белгісіз"

    # Base response
    response = {
        "status": status,
        "date": date_ru,
        "date_kz": date_kz,
        "specialist_name": specialist_name,
        "weekday": weekday_ru,
        "weekday_kz": weekday_kz,
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


def process_reserve_reception_response(response_data, date_obj, requested_time):
    """
    Processes and transforms response from reserve_reception_for_patient function
    with enhanced error handling and status determination.

    Args:
        response_data: Response data from reserve_reception_for_patient
        date_obj: Date object for the appointment
        requested_time: Time requested by user

    Returns:
        dict: Formatted response
    """
    # Determine relation of date to current day
    relation = get_date_relation(date_obj)

    # Check response status
    status = response_data.get("status", "")

    # Extract specialist name
    specialist_name = response_data.get("specialist_name", "Специалист")

    # Special handling for successful booking with success status prefix
    if status in ["success", "success_schedule", "success_change_reception"] or status.startswith(
            "success_change_reception"):
        time = response_data.get("time", requested_time)
        return format_success_scheduling_response(time, date_obj, specialist_name, relation)

    # If requested time is taken and alternatives are suggested
    elif status in ["suggest_times", "error_change_reception"] or status.startswith(
            "error_change_reception") or status.startswith("change_only_"):
        available_times = []

        # Check different field variants with times
        if "suggested_times" in response_data and isinstance(response_data["suggested_times"], list):
            available_times = response_data["suggested_times"]
            # Extract only time from "YYYY-MM-DD HH:MM" format
            available_times = [t.split(" ")[1] if " " in t else t for t in available_times]
        elif "all_available_times" in response_data and isinstance(response_data["all_available_times"], list):
            available_times = response_data["all_available_times"]
            # Extract only time from "YYYY-MM-DD HH:MM" format
            available_times = [t.split(" ")[1] if " " in t else t for t in available_times]
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

        # Remove seconds if present
        available_times = [":".join(t.split(":")[:2]) if t.count(":") == 2 else t for t in available_times]

        # Automatic booking attempt for first available time
        if available_times and requested_time == "10:00" and "перенес" in response_data.get("message", "").lower():
            logger.info(f"Automatic booking attempt for first available time: {available_times[0]}")

            # Format date and time for new attempt
            if " " in available_times[0]:
                time_only = available_times[0].split(" ")[1]
            else:
                time_only = available_times[0]

            new_datetime = f"{date_obj.strftime('%Y-%m-%d')} {time_only}"

            # Attempt new booking
            result = reserve_reception_for_patient(
                patient_id=response_data.get("patient_id", ""),
                date_from_patient=new_datetime,
                trigger_id=1
            )

            # Process result
            if isinstance(result, dict):
                if result.get("status") in ["success_schedule", "success_change_reception"] or result.get(
                        "status").startswith("success_change_reception"):
                    return format_success_scheduling_response(time_only, date_obj, specialist_name, relation)
            elif hasattr(result, 'content'):
                result_dict = json.loads(result.content.decode('utf-8'))
                if result_dict.get("status") in ["success_schedule", "success_change_reception"] or result_dict.get(
                        "status").startswith("success_change_reception"):
                    return format_success_scheduling_response(time_only, date_obj, specialist_name, relation)

        # If automatic attempt failed or wasn't made, return standard response
        return format_error_scheduling_response(available_times, date_obj, specialist_name, relation)

    # If invalid date
    elif status == "error_change_reception_bad_date":
        return {
            "status": "error_change_reception_bad_date",
            "data": response_data.get("message", "Неверная дата")
        }

    # If non-working time
    elif status == "nonworktime":
        return {"status": "nonworktime"}

    # If no slots
    elif status.startswith("error_empty_windows"):
        # Return response unchanged
        return response_data

    # Other errors
    else:
        return {
            "status": "error",
            "message": response_data.get("message", "Произошла ошибка при обработке запроса")
        }


def process_delete_reception_response(response_data):
    """
    Processes and transforms response from delete_reception_for_patient function
    with enhanced error handling.

    Args:
        response_data: Response data from delete_reception_for_patient

    Returns:
        dict: Formatted response
    """
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


def format_date_info(date_obj):
    """
    Formats date in required API format with enhanced parsing.

    Args:
        date_obj: Date object or date string

    Returns:
        dict: Formatted date information
    """
    # If date_obj is string, try to convert to datetime
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

    # Format day, month and weekday
    date_ru = f"{day} {MONTHS_RU[month_num]}"
    date_kz = f"{day} {MONTHS_KZ[month_num]}"
    weekday_ru = WEEKDAYS_RU[weekday]
    weekday_kz = WEEKDAYS_KZ[weekday]

    return {
        "date": date_ru,
        "date_kz": date_kz,
        "weekday": weekday_ru,
        "weekday_kz": weekday_kz
    }
