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


def queue_info():
    """
    Получает очередь, извлекает `queue_id` и `patient_code`,
    затем делает запрос `CLIENT_INFO` для обновления данных пациентов.
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
            <WEB_QUEUE_INFO xmlns="http://sdsys.ru/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:tns="http://sdsys.ru/">
              <MSH>

                <MSH.7>
                  <TS.1>{ts_1}</TS.1>
                </MSH.7>

                <MSH.9>
                  <MSG.1>WEB</MSG.1>
                  <MSG.2>QUEUE_INFO</MSG.2>
                </MSH.9>
                <MSH.10>{msh_10}</MSH.10>
                <MSH.18>UTF-8</MSH.18>
              </MSH>
              <QUEUE_INFO_IN>
                <QUEUEID>{queue_id}</QUEUEID>
                <PCODE>{patient_code}</PCODE>
              </QUEUE_INFO_IN>
            </WEB_QUEUE_INFO>
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
                parse_and_update_queue_info(response.text)
            else:
                logger.error(f"❌ Ошибка {response.status_code} при получении данных для PCODE {patient_code}")

    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении запроса get_queue: {e}")


def normalize_phone(phone):
    """Удаляет +, пробелы, скобки и дефисы, оставляя только цифры."""
    if phone:
        return re.sub(r"[^\d]", "", phone)  # Оставляем только цифры
    return None


def parse_date_string(date_str):
    """
    Преобразует строку даты формата 'YYYYMMDD' в объект datetime.date
    """
    if date_str and len(date_str) == 8:
        try:
            return datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def parse_and_update_queue_info(xml_response):
    """
    Парсит XML-ответ с информацией об очереди и обновляет данные в БД.
    Улучшенная версия с поддержкой связей между моделями.
    """
    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем, что запрос был успешно обработан
        msa_element = root.find(".//ns:MSA/ns:MSA.1", namespace)
        if msa_element is None or msa_element.text != "AA":
            logger.warning("❗ Неуспешный ответ от сервера, код: " +
                           (msa_element.text if msa_element is not None else "Не найден"))
            return

        # Ищем блок с информацией об очереди
        queue_info_element = root.find(".//ns:QUEUE_INFO_OUT/ns:QUEUE_INFO", namespace)
        if queue_info_element is None:
            logger.warning("❗ Нет данных в QUEUE_INFO, обновление не требуется.")
            return

        # Извлекаем основные поля
        queue_id = int(queue_info_element.find("ns:QUEUEID", namespace).text)
        patient_code = int(queue_info_element.find("ns:PCODE", namespace).text)

        # Ищем пациента в базе, если нет - создаем
        patient, _ = Patient.objects.get_or_create(patient_code=patient_code)

        # Обработка причины (ADDID/ADDNAME)
        reason_id_element = queue_info_element.find("ns:ADDID", namespace)
        reason_name_element = queue_info_element.find("ns:ADDNAME", namespace)

        reason = None
        if reason_id_element is not None and reason_id_element.text:
            reason_id = int(reason_id_element.text)
            reason_name = reason_name_element.text if reason_name_element is not None else "Неизвестная причина"

            # Создаем или получаем объект QueueReason
            reason, created = QueueReason.objects.get_or_create(
                reason_id=reason_id,
                defaults={"reason_name": reason_name}
            )

            if not created and reason.reason_name != reason_name:
                # Обновляем название, если оно изменилось
                reason.reason_name = reason_name
                reason.save()

            logger.info(f"{'✅ Создана' if created else '🔄 Использована'} причина: {reason_id} - {reason_name}")

        # Обработка филиалов (FILIAL/TOFILIAL)
        branch = None
        target_branch = None

        # Обработка филиала звонка (FILIAL)
        branch_id_element = queue_info_element.find("ns:FILIAL", namespace)
        branch_name_element = queue_info_element.find("ns:FILIALNAME", namespace)

        if branch_id_element is not None and branch_id_element.text:
            branch_id = int(branch_id_element.text)
            branch_name = branch_name_element.text if branch_name_element is not None else "Неизвестный филиал"

            # Ищем или создаем филиал
            try:
                branch = Clinic.objects.get(clinic_id=branch_id)

                # Обновляем имя клиники, если оно изменилось
                if branch.name != branch_name and branch_name != "Неизвестный филиал":
                    branch.name = branch_name
                    branch.save()
                    logger.info(f"🔄 Обновлено название филиала: {branch_id} → {branch_name}")
            except Clinic.DoesNotExist:
                # Если не существует в БД, создаем новую запись
                branch = Clinic.objects.create(
                    clinic_id=branch_id,
                    name=branch_name,
                    address="",  # Временное значение
                    phone="",  # Временное значение
                    timezone=3  # Временное значение (GMT+3 для России)
                )
                logger.info(f"✅ Создан новый филиал: {branch_id} - {branch_name}")

        # Обработка целевого филиала (TOFILIAL)
        target_branch_id_element = queue_info_element.find("ns:TOFILIAL", namespace)
        target_branch_name_element = queue_info_element.find("ns:TOFILIALNAME", namespace)

        # Приоритет отдается TOFILIAL
        if target_branch_id_element is not None and target_branch_id_element.text:
            target_branch_id = int(target_branch_id_element.text)
            target_branch_name = target_branch_name_element.text if target_branch_name_element is not None else "Неизвестный филиал"

            # Используем TOFILIAL как основной филиал
            branch = None
            try:
                branch = Clinic.objects.get(clinic_id=target_branch_id)

                # Обновляем имя клиники, если оно изменилось
                if branch.name != target_branch_name and target_branch_name != "Неизвестный филиал":
                    branch.name = target_branch_name
                    branch.save()
                    logger.info(f"🔄 Обновлено название целевого филиала: {target_branch_id} → {target_branch_name}")
            except Clinic.DoesNotExist:
                # Если не существует в БД, создаем новую запись
                branch = Clinic.objects.create(
                    clinic_id=target_branch_id,
                    name=target_branch_name,
                    address="",  # Временное значение
                    phone="",  # Временное значение
                    timezone=3  # Временное значение (GMT+3 для России)
                )
                logger.info(f"✅ Создан новый целевой филиал: {target_branch_id} - {target_branch_name}")

        # Собираем данные для QueueInfo с учетом новых связей
        queue_data = {
            "patient": patient,
            "reason": reason,
            "branch": branch,
            "target_branch": target_branch,
        }

        # Добавляем стандартные поля
        fields_mapping = {
            "CURRENTSTATE": ("current_state", int),
            "CURRENTSTATENAME": ("current_state_name", str),
            "DEFAULTNEXTSTATE": ("default_next_state", int),
            "DEFAULTNEXTSTATENAME": ("default_next_state_name", str),
            "CONTACTBDATE": ("contact_start_date", parse_date_string),
            "CONTACTFDATE": ("contact_end_date", parse_date_string),
            "ACTIONBDATE": ("desired_start_date", parse_date_string),
            "ACTIONFDATE": ("desired_end_date", parse_date_string),
            "DCODE": ("doctor_code", int),
            "DNAME": ("doctor_name", str),
            "DEPNUM": ("department_number", int),
            "DEPNAME": ("department_name", str),
        }

        for xml_field, (model_field, convert_func) in fields_mapping.items():
            element = queue_info_element.find(f"ns:{xml_field}", namespace)
            if element is not None and element.text:
                try:
                    queue_data[model_field] = convert_func(element.text)
                except (ValueError, TypeError) as e:
                    logger.warning(f"❗ Ошибка при обработке поля {xml_field}: {e}")

        # Обновляем или создаем запись в таблице QueueInfo
        with transaction.atomic():
            queue_obj, created = QueueInfo.objects.update_or_create(
                queue_id=queue_id,
                defaults=queue_data
            )

            # Обрабатываем контакты очереди
            process_queue_contacts(queue_obj, queue_info_element, namespace)

            if created:
                logger.info(f"✅ Создана новая запись в QueueInfo: {queue_id} для пациента {patient.full_name}")
            else:
                logger.info(f"🔄 Обновлена запись в QueueInfo: {queue_id} для пациента {patient.full_name}")

            # Краткая информация о записи
            reason_info = f" с причиной '{reason.reason_name}'" if reason else " без указания причины"
            branch_info = f" в филиале '{branch.name}'" if branch else ""
            logger.info(f"📋 Информация о записи: Queue {queue_id}{reason_info}{branch_info}")

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке QUEUE_INFO: {e}", exc_info=True)


def process_queue_contacts(queue_obj, queue_info_element, namespace):
    """
    Обрабатывает контакты очереди из XML и сохраняет их в БД.
    """
    # Находим список контактов
    contact_list = queue_info_element.find("ns:QUEUE_CONTACT_LIST", namespace)

    if contact_list is None:
        logger.info(f"ℹ️ Нет контактов для очереди {queue_obj.queue_id}")
        return

    # Получаем все контакты
    contacts = contact_list.findall("ns:QUEUE_CONTACT_INFO", namespace)

    if not contacts:
        logger.info(f"ℹ️ Список контактов пуст для очереди {queue_obj.queue_id}")
        return

    # Удаляем существующие контакты для этой очереди
    QueueContactInfo.objects.filter(queue=queue_obj).delete()

    saved_contacts = 0
    contact_summary = []

    # Сохраняем новые контакты
    for contact in contacts:
        contact_data = {
            "queue": queue_obj,
        }

        # Основные поля контакта
        next_state_element = contact.find(f"ns:NEXTSTATE", namespace)
        next_state_name_element = contact.find(f"ns:NEXTSTATENAME", namespace)

        if next_state_element is not None and next_state_element.text:
            contact_data["next_state"] = int(next_state_element.text)
            if next_state_name_element is not None:
                contact_data["next_state_name"] = next_state_name_element.text
                contact_summary.append(f"{contact_data['next_state']}: {contact_data['next_state_name']}")

        # Маппинг остальных полей контакта
        fields_mapping = {
            "PARENTACTIONID": ("parent_action_id", int),
            "NEXTDCODE": ("next_dcode", int),
            "NEXTDNAME": ("next_dname", str),
            "NEXTCALLDATETIME": ("next_call_datetime", parse_date_string),
        }

        for xml_field, (model_field, convert_func) in fields_mapping.items():
            element = contact.find(f"ns:{xml_field}", namespace)
            if element is not None and element.text:
                try:
                    contact_data[model_field] = convert_func(element.text)
                except (ValueError, TypeError) as e:
                    logger.warning(f"❗ Ошибка при обработке поля контакта {xml_field}: {e}")

        # Создаем новый контакт в БД
        QueueContactInfo.objects.create(**contact_data)
        saved_contacts += 1

    logger.info(f"✅ Сохранено {saved_contacts} вариантов действий для очереди {queue_obj.queue_id}")
    if contact_summary:
        logger.info(f"📊 Возможные действия: {', '.join(contact_summary)}")


def parse_and_update_patient_info(xml_response):
    """
    Парсит XML-ответ `CLIENT_INFO` и обновляет `Patient` в БД.
    Эта функция сохранена для обратной совместимости, если придёт ответ с CLIENT_MAININFO.
    """
    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        client_info = root.find(".//ns:CLIENT_MAININFO", namespace)
        if client_info is None:
            logger.warning("❗ Нет данных в CLIENT_MAININFO, обновление не требуется.")
            return

        patient_code = client_info.find("ns:PCODE", namespace).text
        full_name = client_info.find("ns:PNAME", namespace).text if client_info.find("ns:PNAME",
                                                                                     namespace) is not None else "Unknown"
        address = client_info.find("ns:PADDR", namespace).text if client_info.find("ns:PADDR",
                                                                                   namespace) is not None else None
        phone_mobile = client_info.find("ns:PPHONE", namespace).text if client_info.find("ns:PPHONE",
                                                                                         namespace) is not None else None
        email = client_info.find("ns:PMAIL", namespace).text if client_info.find("ns:PMAIL",
                                                                                 namespace) is not None else None
        gender = int(client_info.find("ns:GENDER", namespace).text) if client_info.find("ns:GENDER",
                                                                                        namespace) is not None else None

        # Нормализуем номер перед сохранением
        phone_mobile = normalize_phone(phone_mobile)

        with transaction.atomic():
            patient, created = Patient.objects.update_or_create(
                patient_code=patient_code,
                defaults={
                    "full_name": full_name,
                    "address": address,
                    "phone_mobile": phone_mobile,
                    "email": email,
                    "gender": gender
                }
            )

            if created:
                logger.info(f"✅ Новый пациент сохранен: {full_name} (PCODE {patient_code}), телефон: {phone_mobile}")
            else:
                logger.info(f"🔄 Данные пациента обновлены: {full_name} (PCODE {patient_code}), телефон: {phone_mobile}")

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке CLIENT_INFO: {e}")


# Запускаем процесс
if __name__ == "__main__":
    queue_info()
