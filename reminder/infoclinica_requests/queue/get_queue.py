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
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')


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
        fdate = (today + timedelta(days=365)).strftime("%Y%m%d")

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
    Обновлено для корректного сохранения информации о целевой клинике (TOFILIAL)
    и использования её для последующих операций.
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

                # Обработка исходного филиала (FILIAL) - больше не используется для операций
                # Не обрабатываем исходный филиал, так как нас интересует только TOFILIAL
                branch = None

                # КРИТИЧЕСКИ ВАЖНО: Обработка целевого филиала (TOFILIAL) - единственная клиника для всех операций
                target_branch = None
                target_branch_id_element = queue_info.find("ns:TOFILIAL", namespace)
                if target_branch_id_element is not None and target_branch_id_element.text:
                    target_branch_id = int(target_branch_id_element.text)
                    target_branch_name_element = queue_info.find("ns:TOFILIALNAME", namespace)
                    target_branch_name = target_branch_name_element.text.strip() if target_branch_name_element is not None else ""

                    # Ищем или создаем целевой филиал
                    target_branch, created = Clinic.objects.get_or_create(
                        clinic_id=target_branch_id,
                        defaults={
                            "name": target_branch_name,
                            "address": "",
                            "phone": "",
                            "timezone": 0
                        }
                    )

                    # Обновляем название, если оно изменилось
                    if not created and target_branch.name != target_branch_name and target_branch_name:
                        target_branch.name = target_branch_name
                        target_branch.save()
                        logger.info(f"🔄 Обновлено название целевого филиала: {target_branch_id} → {target_branch_name}")
                    elif created:
                        logger.info(f"✅ Создан новый целевой филиал: {target_branch_id} - {target_branch_name}")

                    logger.info(f"ВАЖНО: Сохранён TOFILIAL с ID {target_branch_id} для пациента {patient_code}")

                if target_branch is None:
                    logger.warning(
                        f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не указан для пациента {patient_code}. Операции невозможны.")

                current_state = queue_info.find("ns:CURRENTSTATE", namespace)
                current_state = int(current_state.text) if current_state is not None else None

                action_bdate = queue_info.find("ns:ACTIONBDATE", namespace)
                action_bdate = parse_date(action_bdate.text) if action_bdate is not None else None

                action_fdate = queue_info.find("ns:ACTIONFDATE", namespace)
                action_fdate = parse_date(action_fdate.text) if action_fdate is not None else None

                # Обработка информации о докторе
                doctor = None
                doctor_code_element = queue_info.find("ns:DCODE", namespace)
                doctor_name_element = queue_info.find("ns:DNAME", namespace)

                if doctor_code_element is not None and doctor_code_element.text:
                    doctor_code = int(doctor_code_element.text)
                    doctor_name = doctor_name_element.text if doctor_name_element is not None else "Неизвестный доктор"

                    # Найти или создать доктора
                    doctor, created = Doctor.objects.get_or_create(
                        doctor_code=doctor_code,
                        defaults={
                            "full_name": doctor_name,
                            "clinic": target_branch  # ВАЖНО: привязываем к целевой клинике
                        }
                    )

                    if not created and doctor.full_name != doctor_name:
                        # Обновляем имя, если оно изменилось
                        doctor.full_name = doctor_name
                        # Также обновляем клинику, если она другая
                        if doctor.clinic != target_branch and target_branch is not None:
                            doctor.clinic = target_branch
                        doctor.save()
                        logger.info(f"🔄 Обновлено имя доктора: {doctor_code} → {doctor_name}")
                    elif created:
                        logger.info(f"✅ Создан новый доктор: {doctor_code} - {doctor_name}")

                    # Обработка специализации/отделения доктора
                    depnum_element = queue_info.find("ns:DEPNUM", namespace)
                    depname_element = queue_info.find("ns:DEPNAME", namespace)

                    if depnum_element is not None and depnum_element.text:
                        dep_id = int(depnum_element.text)
                        dep_name = depname_element.text if depname_element is not None else "Неизвестное отделение"

                        # Найти или создать отделение
                        department, dept_created = Department.objects.get_or_create(
                            department_id=dep_id,
                            defaults={
                                "name": dep_name,
                                "clinic": target_branch  # ВАЖНО: привязываем к целевой клинике
                            }
                        )

                        if not dept_created and department.name != dep_name:
                            department.name = dep_name
                            # Также обновляем клинику, если она другая
                            if department.clinic != target_branch and target_branch is not None:
                                department.clinic = target_branch
                            department.save()
                            logger.info(f"🔄 Обновлено название отделения: {dep_id} → {dep_name}")
                        elif dept_created:
                            logger.info(f"✅ Создано новое отделение: {dep_id} - {dep_name}")

                        # Связываем доктора с отделением
                        if doctor.department != department:
                            doctor.department = department
                            doctor.save()
                            logger.info(f"🔄 Обновлено отделение доктора {doctor_name}: {department.name}")

                with transaction.atomic():
                    # Создаем или обновляем пациента
                    try:
                        patient = Patient.objects.get(patient_code=patient_code)
                    except Patient.DoesNotExist:
                        # Если пациент не существует, создаем минимальный объект
                        patient = Patient.objects.create(
                            patient_code=patient_code,
                            full_name=f"Пациент {patient_code}"  # Временное имя, будет обновлено позже
                        )
                        logger.info(f"✅ Создан новый пациент с кодом {patient_code}")

                    # Проверяем, есть ли существующие записи для этого пациента и клиники
                    existing_appointments = Appointment.objects.filter(
                        patient=patient,
                        is_active=True,
                        clinic=target_branch if target_branch else None
                    )

                    # Если существует активная запись, обновляем её клинику
                    for appointment in existing_appointments:
                        # Обновляем клинику, если она отличается от целевой
                        if appointment.clinic != target_branch and target_branch is not None:
                            appointment.clinic = target_branch
                            appointment.save()
                            logger.info(
                                f"🔄 Обновлена клиника в записи {appointment.appointment_id} на {target_branch.name}")

                    # Создаем или обновляем запись в очереди
                    queue_data = {
                        "patient": patient,
                        "contact_start_date": contact_bdate,
                        "contact_end_date": contact_fdate,
                        "reason": reason,
                        "branch": None,  # Игнорируем исходный филиал
                        "target_branch": target_branch,  # КРИТИЧЕСКИ ВАЖНО: Целевой филиал используется для MSH.99
                        "current_state": current_state,
                        "desired_start_date": action_bdate,
                        "desired_end_date": action_fdate,
                    }

                    # Добавляем информацию о докторе и отделении если они есть
                    if doctor_code_element is not None and doctor_code_element.text:
                        queue_data["doctor_code"] = int(doctor_code_element.text)
                        queue_data["doctor_name"] = doctor_name

                    if depnum_element is not None and depnum_element.text:
                        queue_data["department_number"] = int(depnum_element.text)
                        queue_data[
                            "department_name"] = depname_element.text if depname_element is not None else "Неизвестное отделение"

                    queue_entry, created = QueueInfo.objects.update_or_create(
                        queue_id=queue_id,
                        defaults=queue_data
                    )

                    if created:
                        logger.info(f"✅ Добавлена новая очередь: {queue_id} для пациента {patient.patient_code}")
                    else:
                        logger.info(f"🔄 Обновлена очередь: {queue_id} для пациента {patient.patient_code}")

                    # Выводим информацию о причине и филиалах с подробностями
                    logger.info(f"📋 Запись в очереди: {queue_id}")
                    logger.info(f"  Пациент: {patient.full_name} (ID: {patient.patient_code})")
                    logger.info(f"  Причина: {reason.reason_name if reason else 'Не указана'}")
                    logger.info(
                        f"  TOFILIAL: {target_branch.name} (ID: {target_branch.clinic_id}) - будет использован в MSH.99" if target_branch else "⚠ TOFILIAL ОТСУТСТВУЕТ!")
                    logger.info(f"  Доктор: {doctor.full_name} (ID: {doctor.doctor_code}) если есть")
                    logger.info(f"  Состояние: {current_state}")

            except Exception as inner_error:
                logger.error(f"⚠ Ошибка обработки записи очереди: {inner_error}")

    except Exception as e:
        logger.error(f"⚠ Ошибка при обработке XML: {e}")


# Запускаем процесс
if __name__ == "__main__":
    get_queue()
