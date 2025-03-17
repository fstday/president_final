import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from dotenv import load_dotenv

# from reminder.acs_requests.trash_orders import trash_orders
from reminder.infoclinica_requests.schedule.schedule_rec_reserve import current_date_time_for_xml
from reminder.infoclinica_requests.utils import compare_times_for_redis, compare_times

load_dotenv()

import requests

import json
import logging
import xml.etree.ElementTree as ET

from requests.auth import HTTPBasicAuth
from datetime import datetime
from logger import logging
from reminder.models import *

logger = logging.getLogger(__name__)
load_dotenv()
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host=os.getenv('INFOCLINICA_HOST')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, '../old_integration/certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def delete_reception_for_patient(patient_id):
    """
    Обновленная функция для удаления записи на прием.
    """
    global clinic_id_msh_99_id
    answer = ''
    result_delete = 0

    try:
        patient = Patient.objects.get(patient_code=patient_id)
        appointment = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time').first()

        if not appointment:
            logger.error(f"Запись на прием для пациента с кодом {patient_id} не найдена")
            return {"status": "error", "message": "Запись на прием не найдена"}

        # Получаем идентификатор клиники
        if appointment.clinic:
            clinic_id_msh_99_id = appointment.clinic.clinic_id
        else:
            # Пытаемся получить из очереди
            queue_entry = QueueInfo.objects.filter(patient=patient).first()
            if queue_entry and queue_entry.branch:
                clinic_id_msh_99_id = queue_entry.branch.clinic_id
            else:
                clinic_id_msh_99_id = 1  # Значение по умолчанию

        logger.info(f"clinic_id_msh_99_id: {clinic_id_msh_99_id}")

        # Определяем ID записи для удаления
        appointment_id = appointment.appointment_id

        # Проверяем, что это ID из Infoclinica
        if not appointment.is_infoclinica_id:
            logger.error(f"ID {appointment_id} не является идентификатором Infoclinica")
            return {"status": "error", "message": "Нельзя удалить запись, не созданную в Infoclinica"}

        # Заголовки запроса
        headers = {
            'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
            'Content-Type': 'text/xml'
        }

        # Формируем XML запрос для удаления записи
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
              <MSH.99>{clinic_id_msh_99_id}</MSH.99> <!-- Идентификатор филиала -->
          </MSH>
          <SCHEDULE_REC_REMOVE_IN>
              <SCHEDID>{appointment_id}</SCHEDID> <!-- Идентификатор назначения -->
          </SCHEDULE_REC_REMOVE_IN>
        </WEB_SCHEDULE_REC_REMOVE>
        '''

        # Выполнение POST-запроса
        response = requests.post(
            url=infoclinica_api_url,
            headers=headers,
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        # Обработка ответа аналогична оригинальной функции
        if response.status_code == 200:
            try:
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                # Извлекаем значение SPRESULT (0 - неудачно, 1 - успешно) и SPCOMMENT (комментарий к результату)
                sp_result_code = root.find('.//ns:SPRESULT', namespace)
                sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

                # Ответ от сервера приходит в текстовом значении, переводим в число
                sp_result = int(sp_result_code.text)

                # Если элементы найдены, проверяем их значения
                if sp_result is not None:
                    # Обработка положительного удаления записи
                    if sp_result == 1:
                        logger.info('Удаление произошло успешно')

                        # Помечаем запись как неактивную вместо полного удаления
                        appointment.is_active = False
                        appointment.save()

                        answer = {
                            'status': 'success_delete',
                            'message': f'Запись по ID приема: {appointment_id}, '
                                       f'ФИО пациента: {patient.full_name}, успешно удалена'
                        }
                        logger.info(answer)

                        return answer

                    elif sp_result == 0:
                        logger.info('Ошибка, удаления не произошло')

                        answer = {
                            'status': 'fail_delete',
                            'message': f'Ошибка, удаление записей на прием за прошедший период запрещено'
                        }
                        logger.info(answer)

                        return answer

                    else:
                        answer = {
                            'status': 'fail_delete',
                            'message': f'Ошибка, пришел неверный код: {patient_id}'
                        }
                        logger.info(answer)

                        return answer

                else:
                    logger.info('Не найдено значений ответа по SPRESULT в ответе сервера')
            except ET.ParseError as e:
                logger.info(f"Ошибка парсинга XML ответа: {e}")
        else:
            logger.info(f'Ошибка при запросе: {response.status_code}')
            logger.info(f'Ответ сервера: {response.text}')

    except Patient.DoesNotExist:
        logger.error(f"Пациент с кодом {patient_id} не найден")
        return {"status": "error", "message": "Пациент не найден"}
    except Exception as e:
        logger.error(f"Ошибка при удалении записи: {str(e)}")
        return {"status": "error", "message": f"Ошибка: {str(e)}"}


delete_reception_for_patient('990000612')
