import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.models import Reception


def add_patient_info():
    contacts = Reception.objects.all()

    if not contacts:
        print('No contacts found')
        return

    for contact in contacts:
        print(f'Processing contact: {contact.full_name} ({contact.phone_number})')

        upload_result = Reception.objects.filter(key=str(contact.phone_number)).last()

        if upload_result:
            order = upload_result.order
            print(f'Found upload result: order {order}')

            audio_info = Reception.objects.filter(order_key=order).first()

            status_info = Reception.objects.filter(order=order).first()

            if audio_info:
                Reception.objects.update_or_create(
                    phone_number=contact.phone_number,
                    full_name=contact.full_name,
                    status=status_info.status_id,
                    link_to_audio=audio_info.audio_link,
                    reception_start_time=contact.reception_start_time,
                )
                print(f'Added PatientInfo for {contact.full_name}')
            else:
                print(f'No audio info for order {order}')
        else:
            print(f'No upload result for phone number {contact.phone_number}')


if __name__ == '__main__':
    add_patient_info()
