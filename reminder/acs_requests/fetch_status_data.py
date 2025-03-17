import requests
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.acs_requests.fetch_audio_data import get_keys_batch
from reminder.models import Appointment, Call
from reminder.properties.utils import ACS_BASE_URL
from reminder.properties.utils import get_latest_api_key


def fetch_status_data(keys_str):
    """
    Fetches status data from the ACS API for the given order keys.

    Args:
        keys_str: Comma-separated string of order keys

    Returns:
        Tuple (data, error) where data is the API response and error is any error message
    """
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
    """
    Fetches BPM actions data from the ACS API for the given order key.

    Args:
        order_key: Order key to fetch BPM actions for

    Returns:
        First BPM action or None if there's an error
    """
    api_key = get_latest_api_key()
    if api_key:
        url = f'{ACS_BASE_URL}/api/v2/orders/public/{api_key}/get_bpm_actions?keys={order_key}'

        try:
            response = requests.get(url)
            response.raise_for_status()
            bpm_data = response.json()
            if bpm_data and isinstance(bpm_data, dict):
                first_action = bpm_data.get(order_key, [])[0]  # Take the first element
                return first_action
        except requests.exceptions.RequestException as e:
            print(f"Error fetching BPM actions for order_key {order_key}: {str(e)}")
    return None


def should_process_order(order_key):
    """
    Determines if an order should be processed based on its BPM actions.

    Args:
        order_key: Order key to check

    Returns:
        True if the order should be processed, False otherwise
    """
    first_action = fetch_bpm_actions_data(order_key)
    if first_action and first_action.get('type') == 'segment':
        return True  # If type is "segment", process the order
    return False  # Otherwise skip it


def process_status_data(status_data_list):
    """
    Processes status data from the API and updates Call records with status IDs.

    Args:
        status_data_list: Status data from the API
    """
    if isinstance(status_data_list, dict):
        for order_key, details in status_data_list.items():
            if isinstance(details, dict):
                # Check if this order should be processed
                if not should_process_order(order_key):
                    print(f"Skipping order_key: {order_key} because the first action type is not 'segment'.")
                    continue  # Skip this order

                for status_group, status_info in details.items():
                    if isinstance(status_info, dict) and 'status_id' in status_info:
                        status_id = status_info['status_id']

                        # Find the Call record by order key
                        call = Call.objects.filter(order_key=order_key).first()

                        if call:
                            call.status_id = status_id
                            call.save()
                            print(f"Updated status for order_key: {order_key} to status_id: {status_id}")

                            # Optionally update the associated appointment's status
                            if call.appointment:
                                call.appointment.status = status_id
                                call.appointment.save()
                                print(f"Updated appointment {call.appointment.appointment_id} status to: {status_id}")
                        else:
                            print(f"Call with order key {order_key} not found.")
            else:
                print(f"Unexpected format for details: {details}")
    else:
        raise ValueError("Unexpected format for status_data_list")


def get_status_data():
    """
    Main function to fetch and process status data for all unprocessed calls.

    Returns:
        Tuple (status_data, error) where status_data is the combined API responses and error is any error message
    """
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
