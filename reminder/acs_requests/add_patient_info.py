import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.models import Appointment, Call, Patient


def add_patient_info():
    """
    Links patient information with appointments by matching phone numbers and updates
    them with status and audio link information from Call records.

    This function has been updated to work with the new database structure.
    """
    # Get all appointments
    appointments = Appointment.objects.filter(is_active=True)

    if not appointments:
        print('No appointments found')
        return

    for appointment in appointments:
        print(f'Processing appointment: {appointment.appointment_id} for patient {appointment.patient.full_name}')

        # Check if the patient has a phone number
        if not appointment.patient.phone_mobile:
            print(f'No phone number for patient: {appointment.patient.full_name}')
            continue

        # Look for calls associated with this appointment
        calls = Call.objects.filter(appointment=appointment)

        if not calls:
            print(f'No calls found for appointment: {appointment.appointment_id}')
            continue

        # Get the latest call
        latest_call = calls.order_by('-created_at').first()

        if latest_call:
            # Update appointment with status and audio link from call
            if latest_call.status_id:
                appointment.status = latest_call.status_id

            if latest_call.audio_link:
                # Audio link is stored in the Call model in the new structure
                print(f'Found audio link for call: {latest_call.audio_link}')

            appointment.save()
            print(f'Updated appointment {appointment.appointment_id} with call information')
        else:
            print(f'No call details found for appointment: {appointment.appointment_id}')


if __name__ == '__main__':
    add_patient_info()
