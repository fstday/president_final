from datetime import datetime

import requests
import os
import django

from collections import defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Kravcov_notif.settings')
django.setup()

from reminder.models import Reception, Call
from reminder.properties.utils import ACS_BASE_URL

from reminder.properties.utils import get_latest_api_key


def fetch_audio_data(keys_str):
    if not keys_str:
        return None, 'No keys found in the database.'

    api_key = get_latest_api_key()
    if api_key:

        url = f'{ACS_BASE_URL}/api/v2/orders/public/{api_key}/get_calls?keys={keys_str}'

        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json(), None
        except requests.exceptions.RequestException as e:
            return None, str(e)


def process_audio_data(audio_data_list):
    if not audio_data_list:
        print("Список аудиоданных пуст.")
        return

    # Словарь для хранения записей по ключу ордера
    audio_data_by_key = defaultdict(list)

    # Группируем записи по ключам ордера
    for audio_data in audio_data_list:
        order_key = audio_data.get('order_key')
        if order_key:
            audio_data_by_key[order_key].append(audio_data)

    # Проходим по сгруппированным записям и выбираем последнюю по времени
    for order_key, audio_records in audio_data_by_key.items():
        # Выбираем запись с самой поздней датой
        last_audio_data = max(audio_records, key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S'))

        audio_link = last_audio_data.get('link')

        if order_key and audio_link:
            # Найдем запись Call по ключу ордера
            call = Call.objects.filter(order_key=order_key).first()

            if call:
                # Обновляем ссылку на аудиозапись
                call.audio_link = audio_link
                call.save()
                print(f"Обновлена аудиоссылка для звонка с ключом: {order_key}")
            else:
                print(f"Звонок с ключом ордера {order_key} не найден.")


def get_audio_data():
    offset = 0
    all_audio_data = []

    while True:
        keys_array = get_keys_batch(batch_size=5, offset=offset)
        if not keys_array:
            break

        keys_str = ','.join(keys_array)
        if not keys_str:
            break

        audio_data_list, error = fetch_audio_data(keys_str)
        if error:
            return [], error

        process_audio_data(audio_data_list)
        all_audio_data.extend(audio_data_list)

        offset += 5

    return all_audio_data, None


"""Below code to get key to last 5 contacts"""


def get_keys_batch(batch_size=5, offset=0):
    # Фильтруем только те звонки, у которых is_added=False
    keys = Call.objects.filter(is_added=False).order_by('-id')[offset:offset + batch_size].values_list('order_key', flat=True)
    return list(keys)


if __name__ == '__main__':
    get_audio_data()
