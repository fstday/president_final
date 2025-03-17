from dotenv import load_dotenv

from reminder.infoclinica_requests.schedule.schedule_rec_reserve import schedule_rec_reserve
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
from logger import logging
from reminder.models import *


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Kravcov_notif.settings')
django.setup()
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


def reserve_reception_for_patient(patient_id, date_from_patient, trigger_id):
    """
    Функция срабатывает после того как GPT направил нам айди - 1 на изменение времени записи клиента. Функция
    отправляет запрос в INFODENT на нахождение ближайших записей у конкретного врача на конкретную дату.
    """


    logger.info(f"🚀 Запуск reserve_reception_for_patient с patient_id={patient_id},"
          f" date_from_patient={date_from_patient}, trigger_id={trigger_id}")

    global target_filial_id
    try:
        found_patient = Patient.objects.get(patient_code=patient_id)
        print(f"✅ Найден пациент: {found_patient}")

        # Debug queue entries
        queue_entries = found_patient.queue_entries.all()
        print(f"Number of queue entries: {queue_entries.count()}")

        latest_queue = found_patient.queue_entries.order_by('-created_at').first()
        print(f"Latest queue entry: {latest_queue}")

        if latest_queue:
            # Extract doctor_code from queue entry
            doctor_id = latest_queue.doctor_code
            print(f"Doctor ID from latest queue: {doctor_id}")

            if not doctor_id:
                print("⚠️ No doctor_code found in the latest queue entry")
                return {
                    'status': 'error',
                    'message': 'Не найден код врача для данного пациента'
                }

            # Get the target clinic ID
            if latest_queue.clinic_id_msh_99:
                target_filial_id = latest_queue.clinic_id_msh_99.clinic_id
                print(f"Patient's target clinic ID: {target_filial_id}")
            else:
                print("⚠️ No clinic_id_msh_99 found in the latest queue entry")
                # You might want to define a default target_filial_id here
        else:
            print("⚠️ No queue entries found for this patient")
            return {
                'status': 'error',
                'message': 'Не найдены записи в очереди для данного пациента'
            }
    except Exception as e:
        print(f"❌ Exception occurred: {str(e)}")
        return {"status": "error", "message": f"Ошибка: {str(e)}"}
    # Парсим даты для отправки XML запроса
    if isinstance(date_from_patient, str):
        date_part, time_part = date_from_patient.split()
        year, month, day = map(int, date_part.split('-'))
        hour, minute = map(int, time_part.split(':'))
        date_obj = datetime(year, month, day, hour, minute)
    elif isinstance(date_from_patient, datetime):
        date_obj = date_from_patient
        date_part = date_obj.strftime('%Y-%m-%d')
        time_part = date_obj.strftime('%H:%M')

    beginning_formatted_date = date_obj.strftime('%Y%m%d')
    time_obj = date_obj.time()

    logger.info(f'formatted_date: {beginning_formatted_date}')
    logger.info(f'date_part: {date_part}, time_part: {time_part}')

    if found_patient:
        logger.info(f'Найдена запись ID: {found_patient}')

        # Заголовки запроса
        headers = {
            'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
            'Content-Type': 'text/xml'
        }

        # XML запрос для получения информации о свободных слотах для записи
        xml_request = f'''
        <WEB_SCHEDULE xmlns="http://sdsys.ru/">
          <MSH>
            <MSH.3></MSH.3>
            <MSH.7>
              <TS.1>{datetime.now().strftime('%Y%m%d%H%M')}</TS.1>
            </MSH.7>
            <MSH.9>
              <MSG.1>WEB</MSG.1>
              <MSG.2>SCHEDULE</MSG.2>
            </MSH.9>
            <MSH.10>f2e89dbc1e813cb680d2f847</MSH.10>
            <MSH.18>UTF-8</MSH.18>
            <MSH.99>{target_filial_id}</MSH.99>
          </MSH>
          <SCHEDULE_IN>
            <INDOCTLIST>{doctor_id}</INDOCTLIST>
            <BDATE>{beginning_formatted_date}</BDATE>
            <FDATE>{beginning_formatted_date}</FDATE>
            <EXTINTERV>30</EXTINTERV> <!-- только для переноса! -->
            <SCHLIST/>
          </SCHEDULE_IN>
        </WEB_SCHEDULE>
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
            root = ET.fromstring(response.text)
            namespace = {'ns': 'http://sdsys.ru/'}

            free_time_intervals = []
            schedint = root.find('.//ns:SCHEDINT', namespace)
            schedident_text = schedint.find('ns:SCHEDIDENT', namespace).text if schedint is not None else None

            for interval in root.findall('.//ns:INTERVAL', namespace):
                bhour = interval.find('ns:BHOUR', namespace).text
                bmin = interval.find('ns:BMIN', namespace).text
                fhour = interval.find('ns:FHOUR', namespace).text
                fmin = interval.find('ns:FMIN', namespace).text
                freetype = interval.find('ns:FREETYPE', namespace).text

                # Выводим данные перед добавлением в список
                logger.info(f"Parsed interval: BHOUR={bhour}, BMIN={bmin}, FHOUR={fhour}, FMIN={fmin}, FREETYPE={freetype}")

                if freetype == '1':  # Только свободные интервалы
                    start_time = f"{bhour}:{bmin.zfill(2)}"
                    end_time = f"{fhour}:{fmin.zfill(2)}"
                    free_time_intervals.append({"start_time": start_time, "end_time": end_time})

            logger.info("Свободные интервалы в формате JSON:")
            logger.info(free_time_intervals)

            # Обработка для trigger_id == 2
            if trigger_id == 2:
                result_time = compare_times_for_redis(free_time_intervals, time_obj, date_part)

                answer = {
                    'status': 'suggest_times',
                    'suggested_times_ten': result_time,
                    'message': f'Данное время {date_from_patient} было занято. Возвращаем ближайшие 10 свободных времен',
                    'action': 'reserve'
                }
                logger.info(answer)

                return answer

            # Обработка для trigger_id == 1
            elif trigger_id == 1:
                result_time = compare_times(free_time_intervals, time_obj, date_part)

                if isinstance(result_time, list):
                    answer = {
                        'status': 'suggest_times',
                        'suggested_times': result_time,
                        'message': f'Данное время {date_from_patient} было занято. Возвращаем ближайшие 10 свободных времен',
                        'action': 'reserve'
                    }

                    logger.info(answer)
                    return answer

                elif result_time:
                    logger.info(f'Найдено подходящее время {result_time}')

                    # Проверяем, есть ли запись на прием для данного пациента
                    try:
                        found_patient = Patient.objects.get(patient_code=patient_id)
                        is_reschedule = found_patient.schedid is not None
                        schedid = found_patient.schedid if is_reschedule else None
                    except Patient.DoesNotExist:
                        # Обработка ошибки, если пациент не найден
                        return {"status": "error", "message": "Пациент не найден"}
                    except Exception as e:
                        logger.error(f"Ошибка при проверке существующих записей: {str(e)}")
                        is_reschedule = False
                        schedid = None

                    answer = schedule_rec_reserve(
                        result_time=result_time,
                        doctor_id=doctor_id,
                        date_part=date_part,
                        patient_id=patient_id,
                        date_obj=date_obj,
                        schedident_text=schedident_text,
                        free_intervals=free_time_intervals,
                        is_reschedule=is_reschedule,
                        schedid=schedid
                    )
                    logger.info(answer)
                    return answer

                else:
                    logger.info('Подходящее время не найдено')
                    answer = {
                        'message': f'Подходящее время не найдено'

                    }
                    return answer

            # Обработка для trigger_id == 3 (когда нужно на конкретный день узнать доступные записи)
            elif trigger_id == 3:
                result_time = compare_times(free_time_intervals, time_obj, date_part)

                if free_time_intervals:
                    return result_time
                else:
                    return None
        else:
            logger.info('Ошибка при запросе:', response.status_code)
            logger.info('Ответ сервера:', response.text)
            return {
                'status': 'error',
                'message': f'Ошибка при запросе: {response.status_code}'
            }

    else:
        logger.info('Запись не найдена')
        return {
            'status': 'error',
            'message': 'Запись не найдена'
        }


patient_code = '990000612'
year_from_patient_for_returning = "2025-03-17 19:00"

reserve_reception_for_patient(patient_code, year_from_patient_for_returning, trigger_id=1)
