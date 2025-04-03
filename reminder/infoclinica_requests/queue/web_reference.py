import os
import django
import re

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
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')
# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def web_reference():
    """
    Получает справочник причин постановки в очередь (WEB_SCHQUEUE_ADDTYPES)
    и сохраняет их в базу данных.
    """
    try:
        # Генерируем метку времени и уникальный идентификатор запроса
        ts_1 = datetime.now().strftime("%Y%m%d%H%M%S")
        msh_10 = generate_msh_10()

        # Формируем XML-запрос на получение справочника
        xml_request = f'''
        <WEB_REFERENCE xmlns="http://sdsys.ru/" xmlns:tns="http://sdsys.ru/">
          <MSH>
            <MSH.7>
              <TS.1>{ts_1}</TS.1>
            </MSH.7>
            <MSH.9>
              <MSG.1>WEB</MSG.1>
              <MSG.2>REFERENCE</MSG.2>
            </MSH.9>
            <MSH.10>{msh_10}</MSH.10>
            <MSH.18>UTF-8</MSH.18>
          </MSH>
          <REFERENCE_IN>
            <REFCODE>WEB_SCHQUEUE_ADDTYPES</REFCODE>
          </REFERENCE_IN>
        </WEB_REFERENCE>
        '''

        logger.info(
            f"\n\n---------------\nОтправка запроса REFERENCE для WEB_SCHQUEUE_ADDTYPES: \n{xml_request}\n---------------\n")

        # Отправляем запрос
        response = requests.post(
            url=infoclinica_api_url,
            headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}', 'Content-Type': 'text/xml'},
            data=xml_request,
            cert=(cert_file_path, key_file_path),
            verify=True
        )

        if response.status_code == 200:
            logger.info(f"\n\n---------------\nОтвет от REFERENCE: {response.text}\n---------------\n")
            parse_reference_response(response.text)
        else:
            logger.error(f"❌ Ошибка {response.status_code} при получении справочника: {response.text}")

    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении запроса web_reference: {e}")


def parse_reference_response(xml_response):
    """
    Парсит XML-ответ от справочника причин постановки в очередь и
    сохраняет/обновляет данные в модели QueueReason.
    """
    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем статус ответа
        sp_result = root.find(".//ns:SPRESULT", namespace)
        if sp_result is None or sp_result.text != "1":
            sp_comment = root.find(".//ns:SPCOMMENT", namespace)
            error_msg = sp_comment.text if sp_comment is not None else "Неизвестная ошибка"
            logger.error(f"❌ Ошибка при получении справочника: {error_msg}")
            return

        # Получаем все записи из справочника
        records = root.findall(".//ns:REC", namespace)
        if not records:
            logger.warning("⚠ В ответе не найдены записи (REC)")
            return

        logger.info(f"📊 Найдено записей в справочнике: {len(records)}")

        # Обрабатываем каждую запись
        with transaction.atomic():
            for rec in records:
                rec_id_elem = rec.find("ns:RECID", namespace)
                rec_name_elem = rec.find("ns:RECNAME", namespace)

                if rec_id_elem is None or rec_name_elem is None:
                    logger.warning("⚠ Пропущена запись без ID или названия")
                    continue

                rec_id = int(rec_id_elem.text)
                rec_name = rec_name_elem.text.strip()

                # Создаем или обновляем QueueReason
                reason, created = QueueReason.objects.update_or_create(
                    reason_id=rec_id,
                    defaults={"reason_name": rec_name}
                )

                if created:
                    logger.info(f"✅ Создана новая причина: {rec_id} - {rec_name}")
                else:
                    logger.info(f"🔄 Обновлена существующая причина: {rec_id} - {rec_name}")

        # Создаем сопоставления причин с внутренними кодами
        create_reason_mappings()

    except Exception as e:
        logger.error(f"❌ Ошибка при парсинге ответа справочника: {e}")


def create_reason_mappings():
    """
    Создает сопоставления между причинами из Инфоклиники и внутренними кодами.
    """
    # Определение сопоставлений
    REASON_MAPPINGS = [
        # Из документации Инфоклиники
        {'add_id': 1, 'internal_code': '0PP0profilac', 'internal_name': 'Программа профилактики'},
        # Профилактический осмотр
        {'add_id': 4, 'internal_code': '00PP0consulta', 'internal_name': 'Консультация'},  # Желание клиента
        {'add_id': 5, 'internal_code': '00PP0prodolzhenie', 'internal_name': 'Лечение продолжить'},
        # Продолжение лечение
        {'add_id': 6, 'internal_code': '00PP0consulta', 'internal_name': 'Консультация'},  # Консультация
        {'add_id': 8, 'internal_code': '0PP0profilac', 'internal_name': 'Программа профилактики'},  # Диспансеризация
        {'add_id': 100000, 'internal_code': '00PP0consulta', 'internal_name': 'Консультация'},  # Лист ожидания
        {'add_id': 111111, 'internal_code': '00PP0rabota', 'internal_name': 'Сдача работы'},
        # Информирование о рез-х согласования услуг

        # Дополнительные сопоставления
        {'add_id': 111113, 'internal_code': '0PP0adapta', 'internal_name': 'Адаптационный визит'},  # Из примера API

        # Стоматологические причины
        {'add_id': -1, 'internal_code': '0PP00plomba', 'internal_name': 'Временная пломба'},
        # Для случаев без явной причины
        {'add_id': 2, 'internal_code': '0PP0coronca', 'internal_name': 'Временная коронка'},
        {'add_id': 3, 'internal_code': '00PP0ydalenie', 'internal_name': 'Удаление'},
        {'add_id': 7, 'internal_code': '0PP0neprishol', 'internal_name': 'Первичный не пришёл на приём'},
        {'add_id': 9, 'internal_code': '0PP0osmotrnarcoz', 'internal_name': 'Осмотр после наркоза'},
        {'add_id': 10, 'internal_code': '0PP0consimp1',
         'internal_name': 'Консультация по имплантации после удаления 1 этап'},
        {'add_id': 11, 'internal_code': '0PP0consimp2',
         'internal_name': 'Консультация по имплантации после удаления 2 этап'},
        {'add_id': 12, 'internal_code': '00PP0protez', 'internal_name': 'Протезирование'},
        {'add_id': 13, 'internal_code': '00PP0gigiena', 'internal_name': 'Гигиена полости рта'},
        {'add_id': 14, 'internal_code': '00PP0narcoz', 'internal_name': 'Наркоз'},
        {'add_id': 15, 'internal_code': '0PP0ortodet', 'internal_name': 'Ортодонтическое лечение до 18'},
        {'add_id': 16, 'internal_code': '0PP0ortodont', 'internal_name': 'Ортодонтическое лечение 18+'},
    ]

    # Создаем сопоставления
    for mapping in REASON_MAPPINGS:
        try:
            reason = QueueReason.objects.get(reason_id=mapping['add_id'])
            mapping_obj, created = QueueReasonMapping.objects.update_or_create(
                reason=reason,
                defaults={
                    'internal_code': mapping['internal_code'],
                    'internal_name': mapping['internal_name']
                }
            )

            if created:
                logger.info(f"✅ Создано сопоставление для причины {reason.reason_name}: {mapping['internal_code']}")
            else:
                logger.info(f"🔄 Обновлено сопоставление для причины {reason.reason_name}: {mapping['internal_code']}")

        except QueueReason.DoesNotExist:
            logger.warning(f"⚠ Причина с ID {mapping['add_id']} не найдена в базе")
        except Exception as e:
            logger.error(f"❌ Ошибка при создании сопоставления для ID {mapping['add_id']}: {e}")


if __name__ == "__main__":
    print("Запуск обновления справочника причин очереди...")
    web_reference()
    print("Обновление завершено.")
