import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from dotenv import load_dotenv
import requests
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from reminder.models import *
from reminder.infoclinica_requests.schedule.schedule_rec_reserve import current_date_time_for_xml
from reminder.infoclinica_requests.utils import compare_times_for_redis, compare_times

logger = logging.getLogger(__name__)
load_dotenv()
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host=os.getenv('INFOCLINICA_HOST')

# Paths to certificates
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def delete_reception_for_patient(patient_id):
    """
    Updated function for deleting an appointment.
    """
    global clinic_id_msh_99_id
    answer = ''
    result_delete = 0

    try:
        patient = Patient.objects.get(patient_code=patient_id)
        appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time').first()

        if not appointment:
            logger.error(f"Appointment for patient with code {patient_id} not found")
            return {"status": "error", "message": "Appointment not found"}

        # Get clinic ID
        if appointment.clinic:
            clinic_id_msh_99_id = appointment.clinic.clinic_id
        else:
            # Try to get from queue
            queue_entry = QueueInfo.objects.filter(patient=patient).first()
            if queue_entry and queue_entry.branch:
                clinic_id_msh_99_id = queue_entry.branch.clinic_id
            else:
                clinic_id_msh_99_id = 1  # Default value

        logger.info(f"clinic_id_msh_99_id: {clinic_id_msh_99_id}")

        # Determine appointment ID for deletion
        appointment_id = appointment.appointment_id

        # Check if this is an ID from Infoclinica
        if not appointment.is_infoclinica_id:
            logger.error(f"ID {appointment_id} is not an Infoclinica identifier")
            return {"status": "error", "message": "Cannot delete appointment not created in Infoclinica"}

        # Request headers
        headers = {
            'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
            'Content-Type': 'text/xml'
        }

        # Format XML request for appointment deletion
        xml_request = f'''
        <WEB_SCHEDULE_REC_REMOVE xmlns="http://sdsys.ru/" xmlns:tns="http://sdsys.ru/">
          <MSH>
              <MSH.7>
              <TS.1>{current_date_time_for_xml}</TS.1>
            </MSH.7>
              <MSH.9>
              <MSG.1>WEB</MSG.1>
                  <MSG.2>SCHEDULE_REC_REMOVE</MSG.2>
            </MSH.9>
              <MSH.10>74C0ACA47AFE4CED2B838996B0DF5821</MSH.10>
            <MSH.18>UTF-8</MSH.18>
              <MSH.99>{clinic_id_msh_99_id}</MSH.99> <!-- Facility ID -->
          </MSH>
          <SCHEDULE_REC_REMOVE_IN>
              <SCHEDID>{appointment_id}</SCHEDID> <!-- Appointment ID -->
          </SCHEDULE_REC_REMOVE_IN>
        </WEB_SCHEDULE_REC_REMOVE>
        '''

        # Execute POST request
        response = requests.post(
            url=infoclinica_api_url,
            headers=headers,
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        # Process response
        if response.status_code == 200:
            try:
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                # Extract SPRESULT (0 - failure, 1 - success) and SPCOMMENT (result comment)
                sp_result_code = root.find('.//ns:SPRESULT', namespace)
                sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

                # Convert server response from text to number
                sp_result = int(sp_result_code.text) if sp_result_code is not None else None

                # If elements are found, check their values
                if sp_result is not None:
                    # Process successful deletion
                    if sp_result == 1:
                        logger.info('Deletion successful')

                        # Mark appointment as inactive instead of fully deleting
                        appointment.is_active = False
                        appointment.save()

                        answer = {
                            'status': 'success_delete',
                            'message': f'Appointment with ID: {appointment_id}, '
                                     f'Patient: {patient.full_name}, successfully deleted'
                        }
                        logger.info(answer)

                        return answer

                    elif sp_result == 0:
                        logger.info('Error, deletion failed')

                        answer = {
                            'status': 'fail_delete',
                            'message': f'Error, deletion of past appointments is not allowed'
                        }
                        logger.info(answer)

                        return answer

                    else:
                        answer = {
                            'status': 'fail_delete',
                            'message': f'Error, invalid code received: {patient_id}'
                        }
                        logger.info(answer)

                        return answer

                else:
                    logger.info('No SPRESULT values found in server response')
            except ET.ParseError as e:
                logger.info(f"Error parsing XML response: {e}")
        else:
            logger.info(f'Request error: {response.status_code}')
            logger.info(f'Server response: {response.text}')

    except Patient.DoesNotExist:
        logger.error(f"Patient with code {patient_id} not found")
        return {"status": "error", "message": "Patient not found"}
    except Exception as e:
        logger.error(f"Error deleting appointment: {str(e)}")
        return {"status": "error", "message": f"Error: {str(e)}"}


delete_reception_for_patient('990000612')
