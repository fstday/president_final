import os
import json
import requests
import base64

from dotenv import load_dotenv
from reminder.models import ApiKey
from datetime import datetime, timedelta
from typing import Optional, List, Dict


load_dotenv()

ACS_BASE_URL = os.getenv('ACS_BASE_URL')
MONTHS_RU = {
    "January": "Января",
    "February": "Февраля",
    "March": "Марта",
    "April": "Апреля",
    "May": "Мая",
    "June": "Июня",
    "July": "Июля",
    "August": "Августа",
    "September": "Сентября",
    "October": "Октября",
    "November": "Ноября",
    "December": "Декабря",
}

MONTHS_KZ = {
    "January": "Қаңтар",
    "February": "Ақпан",
    "March": "Наурыз",
    "April": "Сәуір",
    "May": "Мамыр",
    "June": "Маусым",
    "July": "Шілде",
    "August": "Тамыз",
    "September": "Қыркүйек",
    "October": "Қазан",
    "November": "Қараша",
    "December": "Желтоқсан",
}

DAYS_RU = {
    "Monday": "Понедельник",
    "Tuesday": "Вторник",
    "Wednesday": "Среда",
    "Thursday": "Четверг",
    "Friday": "Пятница",
    "Saturday": "Суббота",
    "Sunday": "Воскресенье",
}

DAYS_KZ = {
    "Monday": "Дүйсенбі",
    "Tuesday": "Сейсенбі",
    "Wednesday": "Сәрсенбі",
    "Thursday": "Бейсенбі",
    "Friday": "Жұма",
    "Saturday": "Сенбі",
    "Sunday": "Жексенбі",
}


def get_latest_api_key():
    latest_api_key = ApiKey.objects.order_by('-updated_at').first()
    return latest_api_key.key if latest_api_key else None


def create_python_script(file_name, data):
    with open(f'{file_name}.py', 'w') as script_file:
        script_file.write("# Auto-generated script\n\n")
        for key, value in data.items():
            if isinstance(value, str):
                script_file.write(f'{key} = "{value}"\n')
            else:
                script_file.write(f'{key} = {value}\n')


def create_response_json_file(file_name, filtered_response):
    with open(f'{file_name}.json', 'w') as json_file:
        json.dump([filtered_response], json_file, indent=4)


def response_result(response, file_name):
    if response.status_code in [200, 201]:
        try:
            response_json = response.json()
            print("Response JSON:")
            print(response_json)

            # create_python_script(file_name, response_json)
            with open(f'{file_name}.json', 'w') as json_file:
                json.dump([response_json], json_file, indent=4)

            # if isinstance(response_json, list):
            #     filtered_response = [item for item in response_json if item.get('removed') == 0]
            #     create_response_json_file('create_patient_list_response', filtered_response)
            # elif isinstance(response_json, dict):
            #
            #     create_response_json_file('create_patient_dict_response', [response_json])
            # else:
            #     print("Unexpected response format")

        except json.JSONDecodeError:
            print("Error decoding JSON response")
        except Exception as e:
            print(f"An error occurred: {e}")
    else:
        print(f"Error: Received status code {response.status_code}")
        print(response.text)


def get_formatted_date_info(dt: datetime) -> Dict[str, str]:
    """
    Принимает дату (datetime) и возвращает отображаемые строки:
      - date: "29 Января"
      - date_kz: "29 Қаңтар"
      - weekday: "Среда"
      - weekday_kz: "Сәрсенбі"
    """
    day_of_month = dt.day
    month_en = dt.strftime("%B")
    weekday_en = dt.strftime("%A")
    month_ru = MONTHS_RU.get(month_en, month_en)
    month_kz = MONTHS_KZ.get(month_en, month_en)
    weekday_ru = DAYS_RU.get(weekday_en, weekday_en)
    weekday_kz = DAYS_KZ.get(weekday_en, weekday_en)
    time = dt.strftime("%H:%M")
    return {
        "date": f"{day_of_month} {month_ru}",
        "date_kz": f"{day_of_month} {month_kz}",
        "weekday": weekday_ru,
        "weekday_kz": weekday_kz,
        "time": time
    }


def is_time_within_working_hours(time_str):
    """
    Validates if a given time is within clinic working hours (09:00-20:30)
    """
    try:
        # Parse the time string to extract hours and minutes
        if isinstance(time_str, int):
            hour = time_str
            minute = 0
        elif ":" in time_str:
            hour, minute = map(int, time_str.split(":"))
        else:
            hour = int(time_str)
            minute = 0

        # Check against working hours (09:00-20:30)
        if hour < 9 or hour > 20:
            return False
        if hour == 20 and minute > 30:
            return False

        return True
    except (ValueError, TypeError):
        return False
