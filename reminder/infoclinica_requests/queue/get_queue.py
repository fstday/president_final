import os
import django

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from dotenv import load_dotenv
from datetime import datetime
from reminder.models import *
from reminder.infoclinica_requests.utils import generate_msh_10
from django.utils.dateparse import parse_date
from django.db import transaction

import requests
import logging
import xml.etree.ElementTree as ET
import pytz
from datetime import datetime, timedelta


# Логирование
logger = logging.getLogger()

# Загрузка переменных окружения
load_dotenv()
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, '../old_integration/certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
infoclinica_x_forwarded_host=os.getenv('INFOCLINICA_HOST')


def get_queue():
    """
    Отправляет запрос на получение очереди и вызывает функцию парсинга.
    """
    try:
        # Генерируем метку времени
        ts_1 = datetime.now().strftime("%Y%m%d%H%M%S")
        msh_10 = generate_msh_10()

        # Даты запроса
        today = datetime.now()
        bdate = today.strftime("%Y%m%d")
        fdate = (today + timedelta(days=60)).strftime("%Y%m%d")

        # XML-запрос
        xml_request = f'''
        <WEB_QUEUE_LIST xmlns="http://sdsys.ru/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:tns="http://sdsys.ru/">
          <MSH>
            <MSH.7>
              <TS.1>{ts_1}</TS.1>
            </MSH.7>
            <MSH.9>
              <MSG.1>WEB</MSG.1>
              <MSG.2>QUEUE_LIST</MSG.2>
            </MSH.9>
            <MSH.10>{msh_10}</MSH.10>
            <MSH.18>UTF-8</MSH.18>
          </MSH>
          <QUEUE_LIST_IN>
            <BDATE>{bdate}</BDATE>
            <FDATE>{fdate}</FDATE>
            <REMTYPE>2</REMTYPE>
          </QUEUE_LIST_IN>
        </WEB_QUEUE_LIST>
        '''

        logger.info(f"\n\n---------------\nОтправка запроса: {xml_request}\n---------------\n")

        # Выполняем запрос
        response = requests.post(
            url=infoclinica_api_url,
            headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}', 'Content-Type': 'text/xml'},
            data=xml_request,
            cert=(cert_file_path, key_file_path),
            verify=True
        )

        if response.status_code == 200:
            logger.info(f"\n\n---------------\nОтвет от get_queue: {response.text}\n---------------\n")
            parse_and_save_queue_info(response.text)
        else:
            logger.error(f"Ошибка {response.status_code}: {response.text}")

    except Exception as e:
        logger.error(f"Ошибка при выполнении запроса get_queue: {e}")


def parse_and_save_queue_info(xml_response):
    """
    Парсит XML-ответ от API Инфоклиника и сохраняет данные в БД.
    Обновлено для работы с новой структурой моделей.
    """
    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        queue_list = root.find(".//ns:QUEUE_LIST", namespace)
        if queue_list is None:
            logger.warning("❗ Нет данных в QUEUE_LIST (возможно, проблема с namespace)")
            return

        for queue_info in queue_list.findall("ns:QUEUE_INFO", namespace):
            try:
                queue_id = int(queue_info.find("ns:QUEUEID", namespace).text)
                patient_code = int(queue_info.find("ns:PCODE", namespace).text)

                contact_bdate = queue_info.find("ns:CONTACTBDATE", namespace)
                contact_bdate = parse_date(contact_bdate.text) if contact_bdate is not None else None

                contact_fdate = queue_info.find("ns:CONTACTFDATE", namespace)
                contact_fdate = parse_date(contact_fdate.text) if contact_fdate is not None else None

                # Обработка причины (ADDID)
                add_id_element = queue_info.find("ns:ADDID", namespace)
                add_id = int(add_id_element.text) if add_id_element is not None else None

                # Получаем или создаем объект QueueReason
                reason = None
                if add_id is not None:
                    reason, _ = QueueReason.objects.get_or_create(
                        reason_id=add_id,
                        defaults={"reason_name": f"Причина {add_id}"}  # Временное имя
                    )

                # Обработка филиалов
                branch = None
                branch_id_element = queue_info.find("ns:FILIAL", namespace)
                if branch_id_element is not None and branch_id_element.text:
                    branch_id = int(branch_id_element.text)
                    branch_name_element = queue_info.find("ns:FILIALNAME", namespace)
                    branch_name = branch_name_element.text.strip() if branch_name_element is not None else ""

                    # Ищем или создаем филиал
                    branch, _ = Clinic.objects.get_or_create(
                        clinic_id=branch_id,
                        defaults={
                            "name": branch_name,
                            "address": "",
                            "phone": "",
                            "timezone": 0  # Временное значение
                        }
                    )

                target_branch = None
                target_branch_id_element = queue_info.find("ns:TOFILIAL", namespace)
                if target_branch_id_element is not None and target_branch_id_element.text:
                    target_branch_id = int(target_branch_id_element.text)
                    target_branch_name_element = queue_info.find("ns:TOFILIALNAME", namespace)
                    target_branch_name = target_branch_name_element.text.strip() if target_branch_name_element is not None else ""

                    # Используем существующий филиал или создаем новый
                    if branch is not None and branch.clinic_id == target_branch_id:
                        target_branch = branch
                    else:
                        target_branch, _ = Clinic.objects.get_or_create(
                            clinic_id=target_branch_id,
                            defaults={
                                "name": target_branch_name,
                                "address": "",
                                "phone": "",
                                "timezone": 0  # Временное значение
                            }
                        )

                current_state = queue_info.find("ns:CURRENTSTATE", namespace)
                current_state = int(current_state.text) if current_state is not None else None

                action_bdate = queue_info.find("ns:ACTIONBDATE", namespace)
                action_bdate = parse_date(action_bdate.text) if action_bdate is not None else None

                action_fdate = queue_info.find("ns:ACTIONFDATE", namespace)
                action_fdate = parse_date(action_fdate.text) if action_fdate is not None else None

                with transaction.atomic():
                    # Создаем или обновляем пациента
                    patient, _ = Patient.objects.get_or_create(patient_code=patient_code)

                    # Обновляем данные о филиалах, если есть дополнительная информация
                    if branch is not None and branch_name_element is not None:
                        branch.name = branch_name_element.text.strip()
                        branch.save()

                    if target_branch is not None and target_branch_name_element is not None:
                        target_branch.name = target_branch_name_element.text.strip()
                        target_branch.save()

                    # Создаем или обновляем запись в очереди
                    queue_entry, created = QueueInfo.objects.update_or_create(
                        queue_id=queue_id,
                        defaults={
                            "patient": patient,
                            "contact_start_date": contact_bdate,
                            "contact_end_date": contact_fdate,
                            "reason": reason,
                            "branch": branch,
                            "target_branch": target_branch,
                            "current_state": current_state,
                            "desired_start_date": action_bdate,
                            "desired_end_date": action_fdate,
                        }
                    )

                    if created:
                        logger.info(f"✅ Добавлена новая очередь: {queue_id} для пациента {patient.patient_code}")
                    else:
                        logger.info(f"🔄 Обновлена очередь: {queue_id} для пациента {patient.patient_code}")

                    # Выводим информацию о причине и филиалах
                    logger.info(f"Причина: {reason.reason_name if reason else 'Не указана'}")
                    logger.info(f"Филиал: {branch.name if branch else 'Не указан'}")
                    logger.info(f"Целевой филиал: {target_branch.name if target_branch else 'Не указан'}")

            except Exception as inner_error:
                logger.error(f"⚠ Ошибка обработки записи очереди: {inner_error}")

    except Exception as e:
        logger.error(f"⚠ Ошибка при обработке XML: {e}")


# Запускаем процесс
if __name__ == "__main__":
    get_queue()
