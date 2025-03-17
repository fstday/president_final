import requests
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.acs_requests.fetch_audio_data import get_keys_batch
from reminder.models import Reception, Call
from reminder.properties.utils import ACS_BASE_URL
from reminder.properties.utils import get_latest_api_key


def fetch_status_data(keys_str):
    if not keys_str:
        return None, 'No keys found in the database.'

    api_key = get_latest_api_key()
    if api_key:
        url = f'{ACS_BASE_URL}/api/v2/orders/public/{api_key}/get_status?keys={keys_str}'

        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json(), None
        except requests.exceptions.RequestException as e:
            return None, str(e)


def fetch_bpm_actions_data(order_key):
    api_key = get_latest_api_key()
    if api_key:
        url = f'{ACS_BASE_URL}/api/v2/orders/public/{api_key}/get_bpm_actions?keys={order_key}'

        try:
            response = requests.get(url)
            response.raise_for_status()
            bpm_data = response.json()
            if bpm_data and isinstance(bpm_data, dict):
                first_action = bpm_data.get(order_key, [])[0]  # Берем первый элемент
                return first_action
        except requests.exceptions.RequestException as e:
            print(f"Error fetching BPM actions for order_key {order_key}: {str(e)}")
    return None


def should_process_order(order_key):
    first_action = fetch_bpm_actions_data(order_key)
    if first_action and first_action.get('type') == 'segment':
        return True  # Если тип "segment", обрабатываем ордер
    return False  # Иначе пропускаем


def process_status_data(status_data_list):
    if isinstance(status_data_list, dict):
        for order_key, details in status_data_list.items():
            if isinstance(details, dict):
                # Проверка, нужно ли обрабатывать этот ордер
                if not should_process_order(order_key):
                    print(f"Skipping order_key: {order_key} because the first action type is not 'segment'.")
                    continue  # Пропускаем данный ордер
                for status_group, status_info in details.items():
                    if isinstance(status_info, dict) and 'status_id' in status_info:
                        status_id = status_info['status_id']
                        # Найдем запись Call по ключу ордера
                        call = Call.objects.filter(order_key=order_key).first()

                        if call:
                            call.status_id = status_id
                            call.save()
                            print(f"Updated status for order_key: {order_key} to status_id: {status_id}")
                        else:
                            print(f"Звонок с ключом ордера {order_key} не найден.")
            else:
                print(f"Unexpected format for details: {details}")
    else:
        raise ValueError("Unexpected format for status_data_list")


def get_status_data():
    offset = 0
    all_status_data = []

    while True:
        keys_array = get_keys_batch(batch_size=5, offset=offset)
        if not keys_array:
            break

        keys_str = ','.join(keys_array)
        if not keys_str:
            break

        status_data_list, error = fetch_status_data(keys_str)
        if error:
            return [], error

        process_status_data(status_data_list)
        all_status_data.extend(status_data_list)

        offset += 5

    return all_status_data, None


if __name__ == '__main__':
    get_status_data()
