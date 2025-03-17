import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Kravcov_notif.settings')
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


def delete_reception_for_patient(patient_code):
    """
    Функция срабатывает после того как GPT направил нам ID - 2 на удаление записи клиента. Функция
    отправляет запрос в INFODENT на нахождение ближайших записей у конкретного врача на конкретную дату.

    :param patient_code: ID записи на прием

    :return: answer
    """

    global clinic_id_msh_99_id
    answer = ''
    result_delete = 0

    # Получаем пациента
    try:
        patient = Patient.objects.get(patient_code=patient_code)
    except Patient.DoesNotExist:
        logger.error(f"Пациент с кодом {patient_code} не найден")
        return {"status": "error", "message": "Пациент не найден"}

    # Получаем филиал из QueueInfo
    queue_entry = QueueInfo.objects.filter(patient=patient).first()
    if queue_entry and queue_entry.clinic_id_msh_99:
        clinic_id_msh_99_id = queue_entry.clinic_id_msh_99.clinic_id
        logger.info(f"clinic_id_msh_99_id: {clinic_id_msh_99_id}")
    else:
        logger.warning("Филиал клиники пациента не найден")

    # Заголовки запроса
    headers = {
        'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
        'Content-Type': 'text/xml'
    }

    # Формируем XML запрос для удаления записи с указанием времени начала приема, если оно доступно
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
          <SCHEDID>{patient.schedid}</SCHEDID> <!-- Идентификатор назначения -->
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

    # Проверка на ошибки и вывод ответа сервера
    if response.status_code == 200:
        try:
            logger.debug(f"Тело ответа: {response.text[:500]}...")  # Логируем первые 500 символов ответа

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
                    patient.delete()

                    answer = {
                        'status': 'success_delete',
                        'message': f'Запись по ID приема: {patient.patient_code}, '
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
                        'message': f'Ошибка, пришел неверный код: {patient_code}'
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


delete_reception_for_patient('990000612')
