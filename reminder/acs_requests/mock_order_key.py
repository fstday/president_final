import os
import django
import requests
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.properties.utils import ACS_BASE_URL, get_latest_api_key


def send_single_patient_to_acs(
        phone,
        full_name,
        gp,
        additional_info
):
    """
    Отправляет одного пациента в ACS систему с возможностью передачи дополнительных параметров.

    :param phone: Номер телефона пациента
    :param full_name: Полное имя пациента
    :param gp: Код причины обращения
    :param additional_info: Словарь с дополнительными параметрами для перезаписи стандартных
    """
    # Получаем API ключ
    api_key = get_latest_api_key()
    if not api_key:
        print("Не удалось получить API ключ ACS")
        return False

    # Базовая структура JSON данных
    json_data = {
        "phone": phone,
        "full_name": full_name,
        "info": {
            "time": "11:30",
            "reception_id": "",
            "patient_code": "990000612",
            "day": "завтра",
            "day_kz": "ертең",
            "weekday": "Четверг",
            "weekday_kz": "Бейсенбі",
            "specialist_code": None,
            "specialization_id": None,
            "specialist_name": "Медицинский Иван Иванович",
            "clinic_id": 12,
            "past_reception_start_time": "2025-04-10 11:30",
            "original_time": "11:30",
            "original_date": "10 Апреля",
            "original_date_kz": "10 Сәуір",
            "gp": gp,
            "cabinet_number": None,
            "service_id": None
        }
    }

    # Перезаписываем стандартные значения, если переданы дополнительные параметры
    if additional_info:
        json_data['info'].update(additional_info)

    print("Отправка данных в формате JSON:")
    print(json.dumps(json_data, indent=2, ensure_ascii=False))

    # URL для отправки
    url = f"{ACS_BASE_URL}/api/v2/bpm/public/bp/{api_key}/add_orders"

    try:
        # Отправляем запрос
        headers = {'Content-Type': 'application/json'}
        response = requests.post(url, json=json_data, headers=headers)

        print(f"Ответ сервера: {response.status_code}")
        if response.text:
            print(f"Текст ответа: {response.text[:200]}...")  # Печатаем первые 200 символов

        # Обработка ответа
        if response.status_code == 200:
            try:
                result_data = response.json()
                print("Успешная отправка в ACS")
                print(f"Ответ: {result_data}")
                return True
            except Exception as e:
                print(f"Ошибка при разборе ответа: {e}")
                return False
        else:
            print(f"Ошибка при отправке в ACS: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        print(f"Ошибка запроса ACS: {e}")
        return False


if __name__ == "__main__":
    # Пример использования с дополнительными параметрами
    send_single_patient_to_acs(
        phone='77070699434',
        full_name='Тест Иван Иванович',
        gp='00PP0consulta',
        additional_info={
            "specialist_code": "12345",
            "specialization_id": "67890",
            "cabinet_number": "101",
            "service_id": "consultation"
        }
    )
