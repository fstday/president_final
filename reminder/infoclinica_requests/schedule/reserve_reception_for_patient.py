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
from reminder.models import *


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
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
    Обновленная функция для работы с новой моделью Appointment.
    """
    logger.info(f"🚀 Запуск reserve_reception_for_patient с patient_id={patient_id}, "
                f"date_from_patient={date_from_patient}, trigger_id={trigger_id}")

    global target_filial_id
    try:
        found_patient = Patient.objects.get(patient_code=patient_id)
        # Ищем активную запись на прием
        existing_appointment = Appointment.objects.filter(
            patient=found_patient,
            is_active=True,
            is_infoclinica_id=True  # Если это запись из Infoclinica
        ).first()
        is_reschedule = existing_appointment is not None
        schedid = existing_appointment.appointment_id if is_reschedule else None

        # Находим последнюю запись на прием для этого пациента
        latest_appointment = Appointment.objects.filter(
            patient=found_patient,
            is_active=True
        ).order_by('-created_at').first()

        if latest_appointment:
            # Получаем код врача из модели Doctor
            if latest_appointment.doctor:
                doctor_id = latest_appointment.doctor.doctor_code
                print(f"Doctor ID from appointment: {doctor_id}")
            else:
                # Если нет врача в модели Appointment, пробуем получить из QueueInfo
                latest_queue = QueueInfo.objects.filter(
                    patient=found_patient
                ).order_by('-created_at').first()

                if latest_queue and latest_queue.doctor_code:
                    doctor_id = latest_queue.doctor_code
                    print(f"Doctor ID from queue: {doctor_id}")
                else:
                    print("⚠️ No doctor_code found")
                    return {
                        'status': 'error',
                        'message': 'Не найден код врача для данного пациента'
                    }

            # Получаем целевую клинику из модели Appointment
            if latest_appointment.clinic:
                target_filial_id = latest_appointment.clinic.clinic_id
                print(f"Target clinic ID from appointment: {target_filial_id}")
            else:
                # Если нет клиники в модели Appointment, пробуем получить из QueueInfo
                latest_queue = QueueInfo.objects.filter(
                    patient=found_patient
                ).order_by('-created_at').first()

                if latest_queue and latest_queue.branch:
                    target_filial_id = latest_queue.branch.clinic_id
                    print(f"Target clinic ID from queue: {target_filial_id}")
                else:
                    target_filial_id = 1  # Значение по умолчанию
                    print(f"Using default target clinic ID: {target_filial_id}")
        else:
            # Если нет записей в модели Appointment, пробуем найти из QueueInfo
            latest_queue = QueueInfo.objects.filter(
                patient=found_patient
            ).order_by('-created_at').first()

            if latest_queue:
                doctor_id = latest_queue.doctor_code
                print(f"Doctor ID from queue: {doctor_id}")

                if latest_queue.branch:
                    target_filial_id = latest_queue.branch.clinic_id
                    print(f"Target clinic ID from queue: {target_filial_id}")
                else:
                    target_filial_id = 1  # Значение по умолчанию
                    print(f"Using default target clinic ID: {target_filial_id}")
            else:
                print("⚠️ No appointments or queue entries found for this patient")
                return {
                    'status': 'error',
                    'message': 'Не найдены записи для данного пациента'
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
