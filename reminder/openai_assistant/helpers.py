import json
import os
from datetime import timedelta

import django

from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()


def has_specific_time(user_input):
    """
    Checks if user input contains a specific time (HH:MM format)

    Args:
        user_input: The user's input text

    Returns:
        bool: True if specific time is mentioned, False otherwise
    """
    import re
    # Check for HH:MM pattern or H:MM pattern
    time_pattern = r'(\d{1,2})[:\s](\d{2})'
    return bool(re.search(time_pattern, user_input))


def has_only_time_period(user_input):
    """
    Checks if user input has time period (morning/afternoon/evening)
    but no specific time

    Args:
        user_input: The user's input text

    Returns:
        bool: True if only time period is mentioned, False otherwise
    """
    time_periods = [
        "утр", "утром", "с утра", "на утро",
        "днем", "обед", "в обед", "на обед",
        "вечер", "вечером", "на вечер", "ужин"
    ]
    user_input_lower = user_input.lower()
    return any(period in user_input_lower for period in time_periods) and not has_specific_time(user_input)


def is_relative_date_request(user_input):
    """
    Checks if user input contains a relative date

    Args:
        user_input: The user's input text

    Returns:
        bool: True if relative date is mentioned, False otherwise
    """
    relative_patterns = [
        "через неделю", "через 3 дня", "через день",
        "через месяц", "через два дня", "через 2 дня",
        "через пару дней", "на следующей неделе"
    ]
    user_input_lower = user_input.lower()
    return any(pattern in user_input_lower for pattern in relative_patterns)


def should_book_automatically(user_input):
    """
    Determines if the assistant should automatically book an appointment
    based on user input

    Args:
        user_input: The user's input text

    Returns:
        bool: True if should auto-book, False if should show options
    """
    # Only auto-book if specific time is mentioned and no time period
    # or relative date is mentioned
    return (has_specific_time(user_input) and
            not is_relative_date_request(user_input))


def handle_booking_request_without_time(patient_code, date_str, date_obj=None):
    """
    Handles booking requests without specific time by directly calling which_time_in_certain_day
    instead of going through reserve_reception_for_patient first

    Args:
        patient_code: Patient's code
        date_str: Date string in YYYY-MM-DD format
        date_obj: Optional datetime object (will be created from date_str if not provided)

    Returns:
        dict: Response with appropriate which_time_* status
    """
    from reminder.openai_assistant.api_views import process_which_time_response
    import re
    from datetime import datetime

    # Create date_obj if not provided
    if date_obj is None and isinstance(date_str, str):
        if date_str == "today":
            date_obj = datetime.now()
        elif date_str == "tomorrow":
            date_obj = datetime.now() + timedelta(days=1)
        elif re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")

    # Call which_time_in_certain_day directly to get available times
    result = which_time_in_certain_day(patient_code, date_str)

    # Process response
    if hasattr(result, 'content'):
        result_dict = json.loads(result.content.decode('utf-8'))
    else:
        result_dict = result

    # Process the result to use the correct status
    processed_result = process_which_time_response(result_dict, date_obj, patient_code)

    # Ensure the status is a which_time_* or only_*_time_* status
    status = processed_result.get("status", "")

    # If the status is from error_change_reception family, convert it to which_time family
    if status.startswith("error_change_reception"):
        if "today" in status:
            processed_result["status"] = "which_time_today"
        elif "tomorrow" in status:
            processed_result["status"] = "which_time_tomorrow"
        else:
            processed_result["status"] = "which_time"

    return processed_result
