import os
import django

# Настройка окружения
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

import logging
import xml.etree.ElementTree as ET
import requests
from datetime import datetime
from django.http import JsonResponse
from dotenv import load_dotenv
from reminder.models import *
from reminder.infoclinica_requests.schedule.schedule_rec_reserve import current_date_time_for_xml

logger = logging.getLogger(__name__)
load_dotenv()

# Конфигурация
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def appointment_time_for_patient(patient_code, year_from_patient_for_returning=None):
    """
    Функция для получения информации о записи пациента.
    Модифицирована для правильного использования TOFILIAL.

    :param patient_code: Код пациента
    :param year_from_patient_for_returning: Дата-время для пациента
    :return: Информация о записи в JSON формате
    """
    try:
        # Находим пациента
        found_patient = Patient.objects.get(patient_code=patient_code)
        logger.info(f"Найден пациент: {found_patient.full_name}")

        # Ищем активные записи пациента
        appointment = Appointment.objects.filter(
            patient=found_patient,
            is_active=True,
            is_infoclinica_id=True  # Только записи из InfoClinica
        ).order_by('-start_time').first()

        if not appointment:
            logger.warning(f"Для пациента {patient_code} не найдены активные записи из InfoClinica")
            # Проверяем, есть ли вообще любые записи
            any_appointment = Appointment.objects.filter(
                patient=found_patient,
                is_active=True
            ).order_by('-start_time').first()

            if not any_appointment:
                logger.error(f"У пациента {patient_code} нет активных записей")
                return {
                    'status': 'error_no_appointment',
                    'time_1': 'null',
                    'message': 'У пациента нет активных записей на прием'
                }
            else:
                # Используем данные из нашей БД
                logger.info(f"Используем локальные данные для записи")
                return get_appointment_info_from_db(any_appointment, year_from_patient_for_returning)

        # Используем ID записи, а не ID пациента
        appointment_id = appointment.appointment_id
        logger.info(f"Найдена активная запись с ID: {appointment_id}")

        # КРИТИЧЕСКИ ВАЖНО: Определяем TOFILIAL для использования в MSH.99
        target_filial_id = 1  # Значение по умолчанию

        # Сначала ищем в текущей записи
        if appointment.clinic:
            target_filial_id = appointment.clinic.clinic_id
            logger.info(f"Используем TOFILIAL из записи: {target_filial_id}")
        else:
            # Если в записи нет клиники, ищем в очереди
            queue_entry = QueueInfo.objects.filter(patient=found_patient).order_by('-created_at').first()
            if queue_entry and queue_entry.target_branch:
                target_filial_id = queue_entry.target_branch.clinic_id
                logger.info(f"Используем TOFILIAL из очереди: {target_filial_id}")
            else:
                logger.warning(f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не найден для пациента {patient_code}!")
                logger.warning("⚠ Используем значение по умолчанию 1, но это может привести к ошибкам!")

        # XML запрос с использованием TOFILIAL в MSH.99
        xml_request = f'''
        <WEB_SCHEDULE_INFO xmlns="http://sdsys.ru/" xmlns:tns="http://sdsys.ru/">
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
              <MSH.99>{target_filial_id}</MSH.99>
          </MSH>
          <SCHEDULE_INFO_IN>
              <SCHEDID>{appointment_id}</SCHEDID>
          </SCHEDULE_INFO_IN>
        </WEB_SCHEDULE_INFO>
        '''

        logger.info(
            f"Отправляем запрос с TOFILIAL={target_filial_id} для получения информации о записи {appointment_id}")
        response = requests.post(
            url=infoclinica_api_url,
            headers={'X-Forwarded-Host': infoclinica_x_forwarded_host, 'Content-Type': 'text/xml'},
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        if response.status_code == 200:
            try:
                logger.info(f"Получен ответ: {response.text}")
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                # Проверяем, есть ли SCHED_INFO в ответе
                sched_info = root.find('.//ns:SCHED_INFO', namespace)

                if sched_info is not None:
                    # Получаем данные из SCHED_INFO
                    bhour_element = sched_info.find('ns:BHOUR', namespace)
                    bmin_element = sched_info.find('ns:BMIN', namespace)

                    if bhour_element is not None and bmin_element is not None:
                        bhour = bhour_element.text
                        bmin = bmin_element.text
                        appointment_time = f"{bhour.zfill(2)}:{bmin.zfill(2)}"

                        # Дополнительная информация
                        workdate_element = sched_info.find('ns:WORKDATE', namespace)
                        dname_element = sched_info.find('ns:DNAME', namespace)
                        fname_element = sched_info.find('ns:FNAME', namespace)

                        workdate = workdate_element.text if workdate_element is not None else "Неизвестно"
                        doctor_name = dname_element.text if dname_element is not None else "Неизвестно"
                        clinic_name = fname_element.text if fname_element is not None else "Неизвестно"

                        # Форматируем дату из YYYYMMDD в YYYY-MM-DD
                        formatted_date = None
                        if workdate and len(workdate) == 8:
                            formatted_date = f"{workdate[:4]}-{workdate[4:6]}-{workdate[6:8]}"

                        # Обрабатываем входное время
                        appointment_time_parts = str(year_from_patient_for_returning).split('+')
                        appointment_time_modified_final = appointment_time_parts[0]

                        return {
                            'status': 'success_appointment',
                            'appointment_id': appointment_id,
                            'appointment_time': appointment_time,
                            'appointment_date': formatted_date,
                            'doctor_name': doctor_name,
                            'clinic_name': clinic_name,
                            'message': f'Запись пациента назначена на {appointment_time}'
                        }

                # Если SCHED_INFO не найден или нет BHOUR/BMIN, проверяем общий результат
                sp_result_element = root.find('.//ns:SPRESULT', namespace)

                if sp_result_element is not None and sp_result_element.text == '1':
                    # Успешный запрос, но нет информации о времени - используем данные из БД
                    logger.info("Запрос успешен, но данные о времени отсутствуют. Используем данные из БД.")
                    return get_appointment_info_from_db(appointment, year_from_patient_for_returning)
                else:
                    # Ошибка запроса
                    sp_comment_element = root.find('.//ns:SPCOMMENT', namespace)
                    sp_comment = sp_comment_element.text if sp_comment_element is not None else "Неизвестная ошибка"

                    logger.error(f"Ошибка запроса: {sp_comment}")
                    return {
                        'status': 'fail_appointment',
                        'time_1': 'null',
                        'message': f'Ошибка получения данных: {sp_comment}'
                    }

            except ET.ParseError as e:
                logger.error(f"Ошибка парсинга XML: {e}")
                return get_appointment_info_from_db(appointment, year_from_patient_for_returning)

        else:
            logger.error(f"Ошибка HTTP: {response.status_code}")
            # В случае ошибки вернуть информацию из БД
            return get_appointment_info_from_db(appointment, year_from_patient_for_returning)

    except Patient.DoesNotExist:
        logger.error(f"Пациент с кодом {patient_code} не найден")
        return {
            'status': 'error',
            'time_1': 'null',
            'message': f'Пациент с кодом {patient_code} не найден'
        }
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        return {
            'status': 'error',
            'time_1': 'null',
            'message': f'Ошибка: {str(e)}'
        }


def get_appointment_info_from_db(appointment, year_from_patient_for_returning):
    """
    Вспомогательная функция для получения информации о записи из базы данных

    :param appointment: Объект записи на прием
    :param year_from_patient_for_returning: Исходная дата-время
    :return: Информация о записи в JSON формате
    """
    try:
        # Форматируем дату и время
        appointment_time = appointment.start_time.strftime('%H:%M')
        appointment_date = appointment.start_time.strftime('%Y-%m-%d')

        # Получаем данные о враче и клинике
        doctor_name = "Неизвестно"
        if appointment.doctor:
            doctor_name = appointment.doctor.full_name

        clinic_name = "Неизвестно"
        if appointment.clinic:
            clinic_name = appointment.clinic.name

        # Обрабатываем входное время
        appointment_time_parts = str(year_from_patient_for_returning).split('+')
        appointment_time_modified_final = appointment_time_parts[0]

        logger.info(f"Возвращаем информацию из БД: прием {appointment.appointment_id} на {appointment_time}")

        return {
            'status': 'success_appointment_from_db',
            'appointment_id': appointment.appointment_id,
            'appointment_time': appointment_time,
            'appointment_date': appointment_date,
            'doctor_name': doctor_name,
            'clinic_name': clinic_name,
            'message': f'Информация из базы данных: запись на {appointment_time}'
        }
    except Exception as e:
        logger.error(f"Ошибка при получении данных из БД: {e}")
        return {
            'status': 'error',
            'time_1': 'null',
            'message': f'Ошибка при получении данных из БД: {str(e)}'
        }


# Для тестирования
if __name__ == "__main__":
    patient_code = '990000612'
    result = appointment_time_for_patient(patient_code)
    print("Результат:", result)
