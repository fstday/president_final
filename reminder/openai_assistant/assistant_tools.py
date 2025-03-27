import json
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

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
RESPONSE_TEMPLATES = {
    # Success responses for appointment rescheduling (Запись 9-1, 9-2)
    "success_change_reception": {
        "status": "success_change_reception",
        "date": "{date}",  # e.g., "29 Января"
        "date_kz": "{date_kz}",  # e.g., "29 Қаңтар"
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",  # e.g., "Пятница"
        "weekday_kz": "{weekday_kz}",  # e.g., "Жұма"
        "time": "{time}",  # e.g., "10:30"
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
        "weekday_kz": "{weekday_kz}",
        "time": "{time}",
        "day": "завтра",
        "day_kz": "ертең"
    },

    # Only one time available (Запись 16.1 - Одно)
    "only_first_time": {
        "status": "only_first_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
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

    # Only two times available (Запись 16.1 - Два)
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

    # No available time slots (Запись 14.1, 16.1 - Занято)
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

    # Available time slots (Запись 14.1, 16.1)
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

    # Successful appointment deletion (Запись 7)
    "success_deleting_reception": {
        "status": "success_deleting_reception",
        "message": "Запись успешно удалена"
    },

    # Error deleting appointment (Запись 7)
    "error_deleting_reception": {
        "status": "error_deleting_reception",
        "message": "{message}"
    },

    # Error rescheduling with alternative times (Запись 11)
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

    # Single alternative time for rescheduling (Запись 11 - Одно)
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

    # Two alternative times for rescheduling (Запись 11 - Два)
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

    # Bad date format (Запись 14.1)
    "error_change_reception_bad_date": {
        "status": "error_change_reception_bad_date",
        "data": "{message}"
    },

    # Other statuses
    "nonworktime": {
        "status": "nonworktime"  # Запись 13
    },
    "bad_user_input": {
        "status": "bad_user_input"  # Запись 30
    },
    "error_med_element": {
        "status": "error_med_element",  # Запись 30
        "message": "{message}"
    },
    "no_action_required": {
        "status": "no_action_required"  # Запись 30
    },
    "error_reception_unavailable": {
        "status": "error_reception_unavailable",
        "message": "{message}"
    },
    "error": {
        "status": "error",
        "message": "{message}"
    },
    "error_starttime_date_not_found": {
        "status": "error_starttime_date_not_found",
        "message": "{message}"
    },
    "error_date_input_not_found": {
        "status": "error_date_input_not_found",
        "message": "{message}"
    },
    "rate_limited": {
        "status": "rate_limited",
        "message": "{message}"
    },
    "error_bad_input": {
        "status": "error_bad_input",  # Запись 30
        "message": "{message}"
    },
    "error_ignored_patient": {
        "status": "error_ignored_patient",
        "message": "{message}"
    }
}


def format_response(status_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats response according to the required format from documentation.
    Improved to correctly handle different status types and ensure they match
    the expected format.

    Args:
        status_type: Response status type
        data: Data to include in response

    Returns:
        dict: Formatted response
    """
    # Определяем динамически статус для сегодня/завтра
    if "_today" not in status_type and "_tomorrow" not in status_type:
        try:
            date_fields = []
            # Находим все поля с датой
            for field in ["date", "appointment_date", "date_time", "date_from_patient"]:
                if field in data and data[field]:
                    date_fields.append(data[field])

            if date_fields:
                # Берем первую найденную дату
                date_str = date_fields[0]

                # Если дата в формате "29 января" - не пытаемся парсить
                if not any(c.isdigit() for c in date_str):
                    pass
                else:
                    # Извлекаем только часть даты, если есть и дата и время
                    if " " in date_str and re.match(r"\d{4}-\d{2}-\d{2}", date_str.split(" ")[0]):
                        date_str = date_str.split(" ")[0]

                    # Понимаем различные форматы даты
                    date_formats = ["%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"]

                    # Пробуем разные форматы
                    date_obj = None
                    for fmt in date_formats:
                        try:
                            date_obj = datetime.strptime(date_str, fmt).date()
                            break
                        except ValueError:
                            continue

                    # Если удалось распознать дату
                    if date_obj:
                        today = datetime.now().date()
                        tomorrow = today + timedelta(days=1)

                        # Дополняем статус информацией о дне
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

    # Проверка на количество доступных времен
    if status_type.startswith("which_time") and not status_type.startswith("which_time_in_certain_day"):
        # Проверяем, сколько времен доступно
        time_fields = ["time_1", "time_2", "time_3", "first_time", "second_time", "third_time"]
        times = []

        for field in time_fields:
            if field in data and data[field]:
                times.append(data[field])

        # Определяем правильный статус в зависимости от количества доступных времен
        if len(times) == 1:
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

    # Получаем шаблон для указанного статуса
    if status_type in RESPONSE_TEMPLATES:
        template = RESPONSE_TEMPLATES[status_type].copy()

        # Заполняем шаблон данными
        for key, value in template.items():
            if isinstance(value, str) and "{" in value and "}" in value:
                field_name = value.strip("{}")
                if field_name in data:
                    template[key] = data[field_name]

        # Добавляем дополнительные поля
        for key, value in data.items():
            if key not in template and key != "status":
                template[key] = value

        return template

    # Если нет соответствующего шаблона, возвращаем исходные данные
    # Но добавляем корректный статус
    if 'status' not in data:
        data['status'] = status_type
    return data
