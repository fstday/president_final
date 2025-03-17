import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from django.http import JsonResponse
from dotenv import load_dotenv

# from reminder.acs_requests.trash_orders import trash_orders
from reminder.infoclinica_requests.schedule.schedule_rec_reserve import current_date_time_for_xml
from reminder.infoclinica_requests.utils import compare_times_for_redis, compare_times

load_dotenv()

import requests
import os
import django
import json
import logging
import xml.etree.ElementTree as ET

from requests.auth import HTTPBasicAuth
from datetime import datetime
from reminder.models import *

logger = logging.getLogger(__name__)
load_dotenv()
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, '../old_integration/certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
infoclinica_x_forwarded_host=os.getenv('INFOCLINICA_HOST')


def appointment_time_for_patient(patient_code, year_from_patient_for_returning):
    """
    Функция срабатывает для поиска времени записи клиента по ID записи

    :param year_from_patient_for_returning:
    :param reception_id:
    :return: result
    """

    all_patients = Patient.objects.all()
    found_patient = all_patients.get(patient_code=patient_code)

    # Проверяем сначала записи на прием
    appointment = Appointment.objects.filter(
        patient=found_patient,
        is_active=True
    ).order_by('-start_time').first()

    if appointment and appointment.clinic:
        target_filial_id = appointment.clinic.clinic_id
    else:
        # Если нет активных записей, пробуем получить из очереди
        latest_queue = found_patient.queue_entries.order_by('-created_at').first()
        if latest_queue:
            # Get the target clinic ID
            if latest_queue.clinic_id_msh_99:
                target_filial_id = latest_queue.clinic_id_msh_99.clinic_id
                print(f"Patient's target clinic ID: {target_filial_id}")
            # Fallback to branch clinic if target is not available
            elif latest_queue.branch:
                target_filial_id = latest_queue.branch.clinic_id
                print(f"Using source clinic ID: {target_filial_id}")
            else:
                # Default clinic ID if nothing found
                target_filial_id = 1
                print(f"No clinic found for patient, using default clinic ID: {target_filial_id}")
        else:
            # Default clinic ID if no queue entries found
            target_filial_id = 1
            print(f"No queue entries found for patient, using default clinic ID: {target_filial_id}")

    # Заголовки запроса
    headers = {
        'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
        'Content-Type': 'text/xml'
    }

    xml_request = f'''
    <WEB_SCHEDULE_INFO  xmlns="http://sdsys.ru/" xmlns:tns="http://sdsys.ru/">
      <MSH>
          <MSH.7>
          <TS.1>{current_date_time_for_xml}</TS.1>
        </MSH.7>
          <MSH.9>
          <MSG.1>WEB</MSG.1>
              <MSG.2>SCHEDULE_INFO</MSG.2>
        </MSH.9>
          <MSH.10>74C0ACA47AFE4CED2B838996B0DF5821</MSH.10>
        <MSH.18>UTF-8</MSH.18>
          <MSH.99>{target_filial_id}</MSH.99> <!-- Идентификатор филиала -->
      </MSH>
      <SCHEDULE_INFO_IN>
          <SCHEDID>{patient_code}</SCHEDID> <!-- Идентификатор назначения -->
      </SCHEDULE_INFO_IN>
    </WEB_SCHEDULE_INFO>
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
            root = ET.fromstring(response.text)
            namespace = {'ns': 'http://sdsys.ru/'}

            # Извлекаем значение SPRESULT (0 - неудачно, 1 - успешно) и SPCOMMENT (комментарий к результату)
            sp_result_code = root.find('.//ns:SPRESULT', namespace)
            sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

            # Извлекаем начало времени приема и конец времени приема
            start_time_hour = root.find('.//ns:BHOUR', namespace)
            start_time_minute = root.find('.//ns:BMIN', namespace)
            finish_time_hour = root.find('.//ns:FHOUR', namespace)
            finish_time_minute = root.find('.//ns:FMIN', namespace)



            # Ответ от сервера приходит в текстовом значении, переводим в число
            sp_result = int(sp_result_code.text)

            # Если элементы найдены, проверяем их значения
            if sp_result is not None:

                # Обработка положительного удаления записи
                if sp_result == 1:
                    logger.info('Получение детальной информации произошло успешно')
                    logging.info(f"\n\nSTART_TIME_HOUR: {start_time_hour}\n\n")
                    if start_time_hour is None or start_time_hour.text is None:
                        logging.error("Элемент 'BHOUR' отсутствует или не содержит текст.")
                        answer = {
                            'status': 'error_empty_windows',
                            'time_1': 'null',
                            'message': 'В день когда пациент хочет записаться, от системы infoclinica не вернулось свободных времен'
                        }
                        return JsonResponse(answer)

                    logger.info(f'{start_time_hour.text.zfill(2)}:{start_time_minute.text.zfill(2)}')

                    final_start_time = f'{start_time_hour.text.zfill(2)}:{start_time_minute.text.zfill(2)}'

                    appointment_time_modified_final, excess_nulls = str(year_from_patient_for_returning).split('+')
                    logger.info(appointment_time_modified_final)
                    answer = {
                        'status': 'success_appointment',
                        'suggested_times': appointment_time_modified_final,
                        'message': f'Ответ пришел успешно, запись пациента назначена на {final_start_time}'
                    }
                    logger.info(answer)

                    return answer

                elif sp_result == 0:
                    logger.info('Ошибка, детальную информацию о времени приема клиента не удалось получить')

                    answer = {
                        'status': 'fail_appointment',
                        'suggested_times': '',
                        'message': 'Ошибка, детальную информацию о времени приема клиента не удалось получить'
                    }
                    logger.info(answer)

                    return answer

                else:
                    answer = {
                        'status': 'fail_appointment',
                        'suggested_times': '',
                        'message': f'Ошибка, пришел неверный код {patient_code}'
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


patient_code = '990000612'
year_from_patient_for_returning = "2025-03-15 14:30:00+03:00"
appointment_time_for_patient(patient_code, year_from_patient_for_returning)
