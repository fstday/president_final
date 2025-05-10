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


def check_if_time_selection_request(user_input, today_slots, tomorrow_slots):
    """
    Checks if user input is a request to select a time from previously displayed times.

    Args:
        user_input: User's input text
        today_slots: List of available time slots for today
        tomorrow_slots: List of available time slots for tomorrow

    Returns:
        bool: True if the request is for time selection, False otherwise
    """
    # Convert user input to lowercase for easier comparison
    user_input_lower = user_input.lower()

    # If there are no available slots, it can't be a time selection request
    if not today_slots and not tomorrow_slots:
        return False

    # Check for common time selection phrases
    selection_phrases = [
        "первое время", "первый вариант", "на первое", "первое",
        "второе время", "второй вариант", "на второе", "второе",
        "третье время", "третий вариант", "на третье", "третье",
        "последнее время", "последний вариант", "на последнее", "последнее",
        "самое раннее", "раннее", "самое позднее", "позднее",
        "номер один", "номер два", "номер три",
        "вариант один", "вариант два", "вариант три",
        "первый", "второй", "третий"
    ]

    # Check for simple agreement phrases after showing times
    agreement_phrases = [
        "да", "хорошо", "ок", "подойдет", "согласен",
        "записывайте", "можно", "запишите", "давайте"
    ]

    # Check for selection phrases
    for phrase in selection_phrases:
        if phrase in user_input_lower:
            return True

    # Check for standalone agreement (only if we have slots)
    if any(word == user_input_lower for word in agreement_phrases):
        return True

    # Check for time phrases with "запишите", "запись", etc.
    booking_keywords = ["запиш", "запись", "записать", "бронир", "возьми"]
    for keyword in booking_keywords:
        if keyword in user_input_lower:
            # Check if any time is mentioned in the request
            for time_slot in today_slots + tomorrow_slots:
                if time_slot in user_input:
                    return True

    return False


def get_selected_time_slot(user_input, today_slots, tomorrow_slots):
    """
    Determines which time slot the user is trying to select based on their input.

    Args:
        user_input: User's input text
        today_slots: List of available time slots for today
        tomorrow_slots: List of available time slots for tomorrow

    Returns:
        tuple: (date_obj, time_str) selected by the user, or (None, None) if unable to determine
    """
    user_input_lower = user_input.lower()
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    # Default to today unless specifically mentioned tomorrow
    target_date = today
    target_slots = today_slots

    # Check if we should use tomorrow's slots
    if "завтра" in user_input_lower:
        target_date = tomorrow
        target_slots = tomorrow_slots

    # If no slots available for target date, check the other day
    if not target_slots:
        if target_date == today and tomorrow_slots:
            target_date = tomorrow
            target_slots = tomorrow_slots
        elif target_date == tomorrow and today_slots:
            target_date = today
            target_slots = today_slots
        else:
            # No slots available for either day
            return None, None

    # Check for specific time selection patterns

    # 1. Ordinal selection (first, second, third)
    if any(phrase in user_input_lower for phrase in ["первое", "первый", "номер один", "вариант один"]):
        if target_slots and len(target_slots) >= 1:
            return target_date, target_slots[0]

    elif any(phrase in user_input_lower for phrase in ["второе", "второй", "номер два", "вариант два"]):
        if target_slots and len(target_slots) >= 2:
            return target_date, target_slots[1]

    elif any(phrase in user_input_lower for phrase in ["третье", "третий", "номер три", "вариант три"]):
        if target_slots and len(target_slots) >= 3:
            return target_date, target_slots[2]

    # 2. Relative position selection
    elif any(phrase in user_input_lower for phrase in ["последнее", "последний", "позднее", "самое позднее"]):
        if target_slots:
            return target_date, target_slots[-1]

    elif any(phrase in user_input_lower for phrase in ["раннее", "самое раннее", "пораньше"]):
        if target_slots:
            return target_date, target_slots[0]

    # 3. Simple agreement (use first option by default)
    elif any(word == user_input_lower for word in
             ["да", "хорошо", "ок", "подойдет", "согласен", "записывайте", "можно"]):
        if target_slots:
            return target_date, target_slots[0]

    # 4. Direct time mention
    for time_slot in target_slots:
        if time_slot in user_input:
            return target_date, time_slot

    # 5. Approximate time mention
    time_period_map = {
        "утр": [slot for slot in target_slots if slot < "12:00"],
        "обед": [slot for slot in target_slots if "12:00" <= slot <= "14:00"],
        "вечер": [slot for slot in target_slots if slot >= "16:00"]
    }

    for period, slots in time_period_map.items():
        if period in user_input_lower and slots:
            return target_date, slots[0]

    # If all else fails but we have context that this is a selection request,
    # default to the first available slot
    if target_slots:
        return target_date, target_slots[0]

    return None, None


def normalize_time(time_str):
    """Нормализует формат времени для корректного сравнения."""
    if ':' in time_str:
        hour, minute = map(int, time_str.split(':'))
        return f"{hour:02d}:{minute:02d}"
    return time_str
