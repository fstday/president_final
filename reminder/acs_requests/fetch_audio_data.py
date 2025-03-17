from datetime import datetime

import requests
import os
import django

from collections import defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.models import Appointment, Call
from reminder.properties.utils import ACS_BASE_URL

from reminder.properties.utils import get_latest_api_key


def fetch_audio_data(keys_str):
    """
    Fetches audio data from the ACS API for the given order keys.

    Args:
        keys_str: Comma-separated string of order keys

    Returns:
        Tuple (data, error) where data is the API response and error is any error message
    """
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
    """
    Processes audio data from the API and updates Call records with audio links.

    Args:
        audio_data_list: List of audio data from the API
    """
    if not audio_data_list:
        print("Audio data list is empty.")
        return

    # Dictionary to store records by order key
    audio_data_by_key = defaultdict(list)

    # Group records by order keys
    for audio_data in audio_data_list:
        order_key = audio_data.get('order_key')
        if order_key:
            audio_data_by_key[order_key].append(audio_data)

    # Process grouped records and select the latest for each
    for order_key, audio_records in audio_data_by_key.items():
        # Select the record with the latest date
        last_audio_data = max(audio_records, key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S'))

        audio_link = last_audio_data.get('link')

        if order_key and audio_link:
            # Find the Call record by order key
            call = Call.objects.filter(order_key=order_key).first()

            if call:
                # Update the audio link
                call.audio_link = audio_link
                call.save()
                print(f"Updated audio link for call with order key: {order_key}")
            else:
                print(f"Call with order key {order_key} not found.")


def get_audio_data():
    """
    Main function to fetch and process audio data for all unprocessed calls.

    Returns:
        Tuple (audio_data, error) where audio_data is the combined API responses and error is any error message
    """
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
    """
    Gets a batch of order keys from Call records that have not been processed.

    Args:
        batch_size: Number of keys to fetch
        offset: Offset to start from

    Returns:
        List of order keys
    """
    # Filter only calls that have is_added=False
    keys = Call.objects.filter(is_added=False).order_by('-id')[offset:offset + batch_size].values_list('order_key',
                                                                                                       flat=True)
    return list(keys)


if __name__ == '__main__':
    get_audio_data()
