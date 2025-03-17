import os
import django
import requests

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.properties.utils import ACS_BASE_URL
from reminder.properties.utils import get_latest_api_key
from reminder.models import Appointment, Call


def send_order(order_data, appointment_id, call_type):
    """
    Обновленная функция для отправки данных заказа в ACS.
    """
    api_key = get_latest_api_key()
    if api_key:
        url = f"{ACS_BASE_URL}/api/v2/bpm/public/bp/{api_key}/add_orders"
        headers = {
            'Content-Type': 'application/json',
        }
        response = requests.post(url, json=order_data, headers=headers)

        if response.status_code == 200:
            try:
                data = response.json().get('data', {})
                if isinstance(data, dict) and data:
                    for phone_number, item in data.items():
                        try:
                            order_key = item.get('order', '')
                            if order_key:
                                # Находим запись на прием по appointment_id
                                appointment = Appointment.objects.filter(appointment_id=appointment_id).first()

                                if appointment:
                                    # Проверяем, есть ли уже звонок для указанного типа
                                    call = Call.objects.filter(appointment=appointment, call_type=call_type).first()

                                    if not call:
                                        # Создаем новый звонок, если не существует
                                        Call.objects.create(
                                            appointment=appointment,
                                            order_key=order_key,
                                            call_type=call_type
                                        )
                                        print(f"Создан новый звонок {call_type} для приема с кодом {appointment_id}")
                                    else:
                                        print(f"Звонок {call_type} для приема с кодом {appointment_id} уже существует.")
                        except Exception as e:
                            print(f"Error updating order: {e}")
                return True  # Успешное выполнение, возвращаем True
            except ValueError as ve:
                print(f"Error decoding JSON: {ve}")
        else:
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as http_err:
                print(f"HTTP error occurred: {http_err}")
    else:
        print("Failed to retrieve or decode API key.")

    return False  # Возвращаем False, если произошла ошибка или запрос не успешен
