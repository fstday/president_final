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
    Функция для удаления записи пациента.
    Модифицирована для правильного использования TOFILIAL.

    :param patient_id: ID пациента
    :return: Результат операции
    """
    answer = ''
    result_delete = 0

    try:
        patient = Patient.objects.get(patient_code=patient_id)
        appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time').first()

        if not appointment:
            logger.error(f"Appointment for patient with code {patient_id} not found")
            return {"status": "error", "message": "Appointment not found"}

        # КРИТИЧЕСКИ ВАЖНО: Получаем TOFILIAL для использования в MSH.99
        target_branch_id = None

        # Сначала проверяем в записи
        if appointment.clinic:
            target_branch_id = appointment.clinic.clinic_id
            logger.info(f"✅ Получен TOFILIAL из записи: {target_branch_id}")
        else:
            # Если в записи нет клиники, ищем в очереди
            queue_entry = QueueInfo.objects.filter(patient=patient).order_by('-created_at').first()
            if queue_entry and queue_entry.target_branch:
                target_branch_id = queue_entry.target_branch.clinic_id
                logger.info(f"✅ Получен TOFILIAL из очереди: {target_branch_id}")

        # Если не найден TOFILIAL, используем значение по умолчанию с предупреждением
        if target_branch_id is None:
            logger.warning(f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не найден для пациента {patient_id}!")
            logger.warning("⚠ Используем значение по умолчанию 1, но это может привести к ошибкам!")
            target_branch_id = 1

        logger.info(f"TOFILIAL для удаления записи: {target_branch_id}")

        # Определение ID записи для удаления
        appointment_id = appointment.appointment_id

        # Проверка, является ли запись из Infoclinica
        if not appointment.is_infoclinica_id:
            logger.error(f"ID {appointment_id} is not an Infoclinica identifier")
            return {"status": "error", "message": "Cannot delete appointment not created in Infoclinica"}

        # Заголовки запроса
        headers = {
            'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
            'Content-Type': 'text/xml'
        }

        # Формируем XML-запрос для удаления записи с TOFILIAL в MSH.99
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
              <MSH.99>{target_branch_id}</MSH.99>
          </MSH>
          <SCHEDULE_REC_REMOVE_IN>
              <SCHEDID>{appointment_id}</SCHEDID>
          </SCHEDULE_REC_REMOVE_IN>
        </WEB_SCHEDULE_REC_REMOVE>
        '''

        logger.info(f"Полный XML-запрос schedule_rec_remove_in: {xml_request}")
        # Выполняем POST-запрос
        logger.info(f"Отправка запроса на удаление записи {appointment_id} с TOFILIAL={target_branch_id}")
        response = requests.post(
            url=infoclinica_api_url,
            headers=headers,
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        # Обрабатываем ответ
        if response.status_code == 200:
            try:
                root = ET.fromstring(response.text)
                logger.debug(f"Тело ответа: {response.text[:500]}...")  # Логируем первые 500 символов ответа

                namespace = {'ns': 'http://sdsys.ru/'}

                # Извлекаем SPRESULT (0 - ошибка, 1 - успех) и SPCOMMENT (комментарий)
                sp_result_code = root.find('.//ns:SPRESULT', namespace)
                sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

                # Преобразуем ответ из текста в число
                sp_result = int(sp_result_code.text) if sp_result_code is not None else None

                # Если элементы найдены, проверяем их значения
                if sp_result is not None:
                    # Успешное удаление
                    if sp_result == 1:
                        logger.info('Deletion successful')

                        # Отмечаем запись как неактивную вместо полного удаления
                        appointment.is_active = False
                        appointment.save()

                        answer = {
                            'status': 'success_delete',
                            'message': f'Appointment with ID: {appointment_id}, '
                                       f'Patient: {patient.full_name}, successfully deleted'
                        }
                        logger.info(answer)

                        return answer

                    # Ошибка удаления
                    elif sp_result == 0:
                        logger.info('Error, deletion failed')
                        sp_comment = sp_comment_text.text if sp_comment_text is not None else "Не указана причина"

                        answer = {
                            'status': 'fail_delete',
                            'message': f'Error: {sp_comment}'
                        }
                        logger.info(answer)

                        return answer

                    # Другой код ответа
                    else:
                        answer = {
                            'status': 'fail_delete',
                            'message': f'Error, invalid code received: {sp_result}'
                        }
                        logger.info(answer)

                        return answer

                # Элементы не найдены
                else:
                    logger.info('No SPRESULT values found in server response')
                    return {"status": "error", "message": "No result code in response"}

            except ET.ParseError as e:
                logger.info(f"Error parsing XML response: {e}")
                return {"status": "error", "message": f"Error parsing response: {str(e)}"}
        else:
            logger.info(f'Request error: {response.status_code}')
            logger.info(f'Server response: {response.text}')
            return {"status": "error", "message": f"HTTP error: {response.status_code}"}

    except Patient.DoesNotExist:
        logger.error(f"Patient with code {patient_id} not found")
        return {"status": "error", "message": "Patient not found"}
    except Exception as e:
        logger.error(f"Error deleting appointment: {str(e)}")
        return {"status": "error", "message": f"Error: {str(e)}"}
