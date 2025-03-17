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


def get_departement_list():
    """
    Метод получает список департаментов от филиала
    """
    try:
        ts_1 = datetime.now().strftime("%Y%m%d%H%M%S")
        msh_10 = generate_msh_10()

        # Извлекаем ВСЕ queue_id и patient_code из QueueInfo
        queue_entries = Clinic.objects.all().values_list("clinic_id", flat=True)

        if not queue_entries:
            logger.info("❗ Нет записей в QueueInfo, обновление не требуется.")
            return

        logger.info(f"📊 Найдено записей в QueueInfo: {len(queue_entries)}")

        for clinic_id in queue_entries:
            if not clinic_id:
                logger.warning(f"⚠ Очередь {clinic_id} не имеет `patient_code`, пропускаем.")
                continue

            # Формируем XML-запрос для получения информации о пациенте
            xml_request = f'''
            <WEB_GET_DEPARTMENT_LIST xmlns="http://sdsys.ru/" xmlns:tns="http://sdsys.ru/">
              <MSH>

                <MSH.7>
                  <TS.1>{ts_1}</TS.1>
                </MSH.7>

                <MSH.9>
                  <MSG.1>WEB</MSG.1>
                  <MSG.2>GET_DEPARTMENT_LIST</MSG.2>
                </MSH.9>
                <MSH.10>{msh_10}</MSH.10>
                <MSH.18>UTF-8</MSH.18>
              </MSH>
              <GET_DEPARTMENT_LIST_IN>
                <FILLIST>{clinic_id}</FILLIST> <!-- Опционально, для получения данных по всем филиалам не передается -->
                <VIEWINWEB>-1</VIEWINWEB> <!--Для локальных CRM-систем передается -1 -->
              </GET_DEPARTMENT_LIST_IN>
            </WEB_GET_DEPARTMENT_LIST>
            '''

            logger.info(
                f"\n\n---------------\nОтправка запроса CLIENT_INFO для PCODE {clinic_id}: \n{xml_request}\n---------------\n")

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
                    f"\n\n---------------\nОтвет от CLIENT_INFO для PCODE {clinic_id}: {response.text}\n---------------\n")
                print_departments(response.text)
                save_departments_to_db(response.text)

            else:
                logger.error(f"❌ Ошибка {response.status_code} парсинге филиалов")

    except Exception as e:
        logger.error(f"❌ Ошибка при выполнении запроса get_queue: {e}")


def print_departments(xml_response):
    """
    Печатает отделения из XML-ответа в удобном формате.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Ищем все отделения в ответе
        departments = root.findall(".//ns:GET_DEPARTMENT_LIST_OUT/ns:GETDEPARTMENTLIST", namespace)
        if not departments:
            print("❗ Нет данных об отделениях в ответе")
            return

        print(f"\n{'=' * 80}")
        print(f"СПИСОК ОТДЕЛЕНИЙ (всего {len(departments)}):")
        print(f"{'=' * 80}")

        for dept in departments:
            dept_id = dept.find("ns:DEPNUM", namespace).text
            dept_name = dept.find("ns:DEPNAME", namespace).text if dept.find("ns:DEPNAME",
                                                                             namespace) is not None else "Неизвестно"

            # Группа отделений
            group_elem = dept.find("ns:DEPGRPNAME", namespace)
            group_name = group_elem.text if group_elem is not None else "Не указана"

            # Видимость на сайте
            viewinweb_elem = dept.find("ns:VIEWINWEB_OUT", namespace)
            viewinweb = "Да" if viewinweb_elem is not None and viewinweb_elem.text == "1" else "Нет"

            # Медиа ID
            media_elem = dept.find("ns:MEDIAID", namespace)
            media_id = media_elem.text if media_elem is not None else "Нет"

            # Избранное
            favorite_elem = dept.find("ns:ISFAVORITE", namespace)
            is_favorite = "Да" if favorite_elem is not None and favorite_elem.text == "1" else "Нет"

            print(f"\n{'-' * 80}")
            print(f"Отделение ID: {dept_id} | {dept_name}")
            print(f"Группа: {group_name}")
            print(f"Отображается на сайте: {viewinweb}")
            print(f"Медиа ID: {media_id}")
            print(f"Избранное: {is_favorite}")

        print(f"\n{'=' * 80}\n")

    except Exception as e:
        print(f"❌ Ошибка при обработке данных об отделениях: {e}")


def save_departments_to_db(xml_response, clinic_id=None):
    """
    Парсит XML-ответ с информацией об отделениях и сохраняет их в БД.

    Параметры:
    xml_response - XML-ответ от сервера
    clinic_id - ID клиники (филиала), к которой относятся отделения (может быть None)
    """
    import xml.etree.ElementTree as ET
    from django.db import transaction
    from reminder.models import Department, Clinic

    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем успешность запроса
        msa_element = root.find(".//ns:MSA/ns:MSA.1", namespace)
        if msa_element is None or msa_element.text != "AA":
            print("❗ Неуспешный ответ от сервера.")
            return 0, 0

        # Ищем все отделения в ответе
        departments = root.findall(".//ns:GET_DEPARTMENT_LIST_OUT/ns:GETDEPARTMENTLIST", namespace)
        if not departments:
            print("❗ Нет данных об отделениях в ответе")
            return 0, 0

        # Получаем объект клиники, если указан ID
        clinic = None
        if clinic_id:
            try:
                clinic = Clinic.objects.get(clinic_id=clinic_id)
            except Clinic.DoesNotExist:
                print(f"⚠️ Клиника с ID {clinic_id} не найдена в базе данных")

        created_count = 0
        updated_count = 0

        # Сохраняем в базу данных
        with transaction.atomic():
            for dept in departments:
                # Получаем основные данные
                dept_id = int(dept.find("ns:DEPNUM", namespace).text)
                dept_name_elem = dept.find("ns:DEPNAME", namespace)
                dept_name = dept_name_elem.text if dept_name_elem is not None else "Неизвестное отделение"

                # Собираем все необязательные поля
                dept_data = {
                    'name': dept_name,
                    'clinic': clinic,  # Связь с клиникой
                }

                # Группа отделений
                group_elem = dept.find("ns:DEPGRPNAME", namespace)
                if group_elem is not None:
                    dept_data['group_name'] = group_elem.text

                # Видимость на сайте
                viewinweb_elem = dept.find("ns:VIEWINWEB_OUT", namespace)
                if viewinweb_elem is not None:
                    dept_data['view_in_web'] = viewinweb_elem.text == "1"

                # Медиа ID
                media_elem = dept.find("ns:MEDIAID", namespace)
                if media_elem is not None:
                    dept_data['media_id'] = media_elem.text

                # Избранное
                favorite_elem = dept.find("ns:ISFAVORITE", namespace)
                if favorite_elem is not None:
                    dept_data['is_favorite'] = favorite_elem.text == "1"

                # Комментарий
                comment_elem = dept.find("ns:COMMENT", namespace)
                if comment_elem is not None:
                    dept_data['comment'] = comment_elem.text

                # Создаем или обновляем запись в БД
                department, created = Department.objects.update_or_create(
                    department_id=dept_id,
                    defaults=dept_data
                )

                if created:
                    created_count += 1
                    print(f"✅ Создано новое отделение: {dept_id} - {dept_name}")
                else:
                    updated_count += 1
                    print(f"🔄 Обновлено отделение: {dept_id} - {dept_name}")

        clinic_info = f" для клиники {clinic.name} (ID: {clinic_id})" if clinic else ""
        print(f"💾 Итого{clinic_info}: создано {created_count}, обновлено {updated_count} отделений")
        return created_count, updated_count

    except Exception as e:
        print(f"❌ Ошибка при сохранении отделений в БД: {e}")
        import traceback
        traceback.print_exc()
        return 0, 0


# Запускаем процесс
if __name__ == "__main__":
    get_departement_list()
