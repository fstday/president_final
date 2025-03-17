import os
import django
import re

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from dotenv import load_dotenv
from datetime import datetime
from reminder.models import *
from reminder.utils.utils import generate_msh_10
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
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, '../old_integration/certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def get_filial_list():
    """
    Метод получает список филиалов.
    """

    try:
        ts_1 = datetime.now().strftime("%Y%m%d%H%M%S")
        msh_10 = generate_msh_10()

        # Извлекаем ВСЕ queue_id и patient_code из QueueInfo
        queue_entries = QueueInfo.objects.all().values_list("queue_id", "patient__patient_code")

        if not queue_entries:
            logger.info("❗ Нет записей в QueueInfo, обновление не требуется.")
            return

        logger.info(f"📊 Найдено записей в QueueInfo: {len(queue_entries)}")

        for queue_id, patient_code in queue_entries:
            if not patient_code:
                logger.warning(f"⚠ Очередь {queue_id} не имеет `patient_code`, пропускаем.")
                continue

            # Формируем XML-запрос для получения информации о пациенте
            xml_request = f'''
            <WEB_GET_FILIAL_LIST xmlns="http://sdsys.ru/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:tns="http://sdsys.ru/">
              <MSH>

                <MSH.7>
                  <TS.1>{ts_1}</TS.1>
                </MSH.7>

                <MSH.9>
                  <MSG.1>WEB</MSG.1>
                  <MSG.2>GET_FILIAL_LIST</MSG.2>
                </MSH.9>
                <MSH.10>{msh_10}</MSH.10>
                <MSH.18>UTF-8</MSH.18>
              </MSH>
              <GET_FILIAL_LIST_IN>
                <FILLIST>-1</FILLIST>
                <VIEWINWEB>-1</VIEWINWEB>
                <IGNORESHOWCASHREF>1</IGNORESHOWCASHREF>
              </GET_FILIAL_LIST_IN>
            </WEB_GET_FILIAL_LIST>
            '''

            logger.info(
                f"\n\n---------------\nОтправка запроса CLIENT_INFO для PCODE {patient_code}: \n{xml_request}\n---------------\n")

            # Отправляем запрос
            response = requests.post(
                url=infoclinica_api_url,
                headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}', 'Content-Type': 'text/xml'},
                data=xml_request,
                cert=(cert_file_path, key_file_path),
                verify=True
            )

            if response.status_code == 200:
                logger.info(
                    f"\n\n---------------\nОтвет от CLIENT_INFO для PCODE {patient_code}: {response.text}\n---------------\n")
                print_filials(response.text)
                save_filials_to_db(response.text)
            else:
                logger.error(f"❌ Ошибка {response.status_code} парсинге филиалов")

    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении запроса get_queue: {e}")


def print_filials(xml_response):
    """
    Просто печатает филиалы из XML-ответа в удобном формате.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Ищем все филиалы в ответе
        filials = root.findall(".//ns:GET_FILIAL_LIST_OUT/ns:GETFILIALLIST", namespace)
        if not filials:
            print("❗ Нет данных о филиалах в ответе")
            return

        print(f"\n{'=' * 80}")
        print(f"СПИСОК ФИЛИАЛОВ (всего {len(filials)}):")
        print(f"{'=' * 80}")

        for filial in filials:
            filial_id = filial.find("ns:FILIAL", namespace).text
            filial_name = filial.find("ns:FNAME", namespace).text if filial.find("ns:FNAME",
                                                                                 namespace) is not None else "Неизвестно"

            # Адрес
            address_elem = filial.find("ns:FADDR", namespace)
            address = address_elem.text if address_elem is not None else "Адрес не указан"

            # Телефон
            phone_elem = filial.find("ns:FPHONE", namespace)
            phone = phone_elem.text if phone_elem is not None else "Телефон не указан"

            # Видимость на сайте
            viewinweb_elem = filial.find("ns:VIEWINWEB_OUT", namespace)
            viewinweb = "Да" if viewinweb_elem is not None and viewinweb_elem.text == "1" else "Нет"

            # Часовой пояс
            timezone_elem = filial.find("ns:TIMEZONE", namespace)
            timezone = f"UTC+{timezone_elem.text}" if timezone_elem is not None else "Не указан"

            # Время работы
            hours_elem = filial.find("ns:WORKHOURS", namespace)
            work_hours = hours_elem.text if hours_elem is not None else "Не указано"

            print(f"\n{'-' * 80}")
            print(f"Филиал ID: {filial_id} | {filial_name}")
            print(f"Адрес: {address}")
            print(f"Телефон: {phone}")
            print(f"Часовой пояс: {timezone}")
            print(f"Отображается на сайте: {viewinweb}")
            print(f"Время работы: {work_hours}")

        print(f"\n{'=' * 80}\n")

    except Exception as e:
        print(f"❌ Ошибка при обработке данных о филиалах: {e}")


def save_filials_to_db(xml_response):
    """
    Парсит XML-ответ с информацией о филиалах и сохраняет их в БД.
    """
    import xml.etree.ElementTree as ET
    from django.db import transaction
    from reminder.models import Clinic

    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем успешность запроса
        msa_element = root.find(".//ns:MSA/ns:MSA.1", namespace)
        if msa_element is None or msa_element.text != "AA":
            print("❗ Неуспешный ответ от сервера.")
            return

        # Ищем все филиалы в ответе
        filials = root.findall(".//ns:GET_FILIAL_LIST_OUT/ns:GETFILIALLIST", namespace)
        if not filials:
            print("❗ Нет данных о филиалах в ответе")
            return

        created_count = 0
        updated_count = 0

        # Сохраняем в базу данных
        with transaction.atomic():
            for filial in filials:
                filial_id = int(filial.find("ns:FILIAL", namespace).text)
                filial_name = filial.find("ns:FNAME", namespace).text if filial.find("ns:FNAME",
                                                                                     namespace) is not None else "Неизвестно"

                # Необязательные поля, собираем их если они есть
                address_elem = filial.find("ns:FADDR", namespace)
                address = address_elem.text if address_elem is not None else ""

                phone_elem = filial.find("ns:FPHONE", namespace)
                phone = phone_elem.text if phone_elem is not None else ""

                email_elem = filial.find("ns:FMAIL", namespace)
                email = email_elem.text if email_elem is not None else None

                timezone_elem = filial.find("ns:TIMEZONE", namespace)
                timezone = int(timezone_elem.text) if timezone_elem is not None else 3  # По умолчанию Москва (UTC+3)

                # Пытаемся найти существующий филиал или создать новый
                clinic, created = Clinic.objects.update_or_create(
                    clinic_id=filial_id,
                    defaults={
                        'name': filial_name,
                        'address': address,
                        'phone': phone,
                        'email': email,
                        'timezone': timezone
                    }
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

        print(f"✅ Сохранено в базу данных: создано {created_count}, обновлено {updated_count} филиалов")
        return created_count, updated_count

    except Exception as e:
        print(f"❌ Ошибка при сохранении филиалов в БД: {e}")
        return 0, 0


# Запускаем процесс
if __name__ == "__main__":
    get_filial_list()
