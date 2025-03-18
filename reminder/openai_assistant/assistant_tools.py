import json
from typing import List, Dict, Any

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
                    "reception_id": {
                        "type": "string",
                        "description": "Patient identifier code (patient_code)"
                    },
                    "date_time": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format to check for available times, or 'today' for current day"
                    }
                },
                "required": ["reception_id", "date_time"]
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
    # Success responses for appointment rescheduling
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

    # Only one time available
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

    # Only two times available
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

    # No available time slots
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

    # Available time slots
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

    # Successful appointment deletion
    "success_deleting_reception": {
        "status": "success_deleting_reception",
        "message": "Запись успешно удалена"
    },

    # Error deleting appointment
    "error_deleting_reception": {
        "status": "error_deleting_reception",
        "message": "{message}"
    },

    # Error rescheduling with alternative times
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

    # Single alternative time for rescheduling
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

    # Two alternative times for rescheduling
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

    # Bad date format
    "error_change_reception_bad_date": {
        "status": "error_change_reception_bad_date",
        "data": "{message}"
    },

    # Other statuses
    "nonworktime": {
        "status": "nonworktime"
    },
    "bad_user_input": {
        "status": "bad_user_input"
    },
    "error": {
        "status": "error",
        "message": "{message}"
    },
    "error_med_element": {
        "status": "error_med_element",
        "message": "{message}"
    },
    "error_reception_unavailable": {
        "status": "error_reception_unavailable",
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
        "status": "error_bad_input",
        "message": "{message}"
    },
    "error_ignored_patient": {
        "status": "error_ignored_patient",
        "message": "{message}"
    }
}


def format_response(status_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats response according to the required format from documentation

    Args:
        status_type: Response status type
        data: Data to include in response

    Returns:
        dict: Formatted response
    """
    if status_type in RESPONSE_TEMPLATES:
        template = RESPONSE_TEMPLATES[status_type].copy()

        # Fill template with data
        for key, value in template.items():
            if isinstance(value, str) and "{" in value and "}" in value:
                field_name = value.strip("{}")
                if field_name in data:
                    template[key] = data[field_name]

        # Add additional fields if they exist in data but not in template
        for key, value in data.items():
            if key not in template and key != "status":
                template[key] = value

        return template

    # If there's no template, return data as is
    return data
