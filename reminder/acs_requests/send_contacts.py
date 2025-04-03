import os
import django
import requests
import json
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.properties.utils import ACS_BASE_URL, get_latest_api_key
from reminder.models import Appointment, Call


def send_order(appointment_id: int, call_type: str) -> bool:
    """
    Создает заказ в ACS и сохраняет order_key в модели Call.
    """
    api_key = get_latest_api_key()
    if not api_key:
        print("Не удалось получить API ключ ACS")
        return False

    # Получаем объект записи на прием
    appointment = Appointment.objects.filter(appointment_id=appointment_id).first()
    if not appointment:
        print(f"Appointment with ID {appointment_id} not found.")
        return False

    patient = appointment.patient
    doctor = appointment.doctor
    clinic = appointment.clinic
    department = appointment.department

    # Обработка номера телефона
    phone = patient.phone_mobile or ''
    phone = ''.join(filter(str.isdigit, phone))
    if phone.startswith('8'):
        phone = '7' + phone[1:]
    elif not phone.startswith('7'):
        phone = '7' + phone

    # Формирование payload
    payload = {
        "phone": phone,
        "project_id": os.getenv("ACS_PROJECT_ID"),
        "attributes": {
            "patient_name": patient.get_full_name(),
            "appointment_date": appointment.start_time.strftime("%d.%m.%Y"),
            "appointment_time": appointment.start_time.strftime("%H:%M"),
            "doctor_name": doctor.full_name if doctor else "Врач",
            "clinic_name": clinic.name if clinic else "",
            "clinic_address": clinic.address if clinic else "",
            "department_name": department.name if department else "",
            "reception_id": str(appointment.appointment_id),
            "call_type": call_type,
        }
    }

    url = f"{ACS_BASE_URL}/api/v2/bpm/public/bp/{api_key}/add_orders"
    headers = {
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(url, json=payload, headers=headers)

        if response.status_code == 200:
            result_data = response.json().get('data', {})
            order_key = result_data.get(phone, {}).get('order')

            if order_key:
                call, created = Call.objects.get_or_create(
                    appointment=appointment,
                    call_type=call_type,
                    defaults={"order_key": order_key}
                )
                if created:
                    print(f"Создан новый звонок {call_type} для приема {appointment_id}")
                else:
                    print(f"Звонок {call_type} для приема {appointment_id} уже существует")
                return True
            else:
                print(f"Ответ ACS не содержит order_key для телефона {phone}: {result_data}")
        else:
            print(f"Ошибка при отправке в ACS: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"Ошибка при выполнении запроса в ACS: {e}")

    return False
