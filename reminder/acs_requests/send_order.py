import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

import requests
import json
import traceback
from django.utils.timezone import now
from reminder.properties.utils import ACS_BASE_URL, get_latest_api_key
from reminder.models import Appointment, Call, QueueInfo, Patient


def process_queue_to_acs():
    """
    Обрабатывает активные записи в очереди и отправляет их в ACS систему.
    Использует метод отправки плоского JSON, который точно определяет все поля.
    """
    api_key = get_latest_api_key()
    if not api_key:
        print("Не удалось получить API ключ ACS")
        return False

    active_queue_entries = QueueInfo.objects.all().select_related('patient', 'target_branch').order_by('-created_at')
    print(f"Найдено {active_queue_entries.count()} активных записей в очереди")

    success_count = 0
    error_count = 0

    for queue_entry in active_queue_entries:
        patient = queue_entry.patient
        if not patient:
            print(f"⚠ Очередь {queue_entry.queue_id} не имеет пациента, пропускаем.")
            error_count += 1
            continue

        if not patient.phone_mobile:
            print(f"⚠ Пациент {patient.patient_code} не имеет номера телефона, пропускаем.")
            error_count += 1
            continue

        queue_reason_code = (
                queue_entry.internal_reason_code or
                getattr(patient, 'last_queue_reason_code', None) or
                "00PP0consulta"
        )

        print(f"✅ Используем причину: {queue_reason_code}")

        # Обработка номера телефона
        phone = ''.join(filter(str.isdigit, patient.phone_mobile))
        if phone.startswith('8'):
            phone = '7' + phone[1:]
        elif not phone.startswith('7'):
            phone = '7' + phone

        # Базовые данные приема
        doctor_name = "Не назначен"
        clinic_name = ""
        clinic_address = ""
        department_name = ""
        appointment_date = "Не назначено"
        appointment_time = "Не назначено"
        appointment_id = None

        try:
            # Поиск связанного приема
            appointment = Appointment.objects.filter(
                patient=patient,
                is_active=True,
                start_time__gt=now()
            ).order_by('start_time').first()

            if appointment:
                doctor_name = str(appointment.doctor.full_name) if appointment.doctor else "Не назначен"
                clinic_name = str(appointment.clinic.name) if appointment.clinic else ""
                clinic_address = str(appointment.clinic.address) if appointment.clinic else ""
                department_name = str(appointment.department.name) if appointment.department else ""
                appointment_date = appointment.start_time.strftime("%d.%m.%Y")
                appointment_time = appointment.start_time.strftime("%H:%M")
                appointment_id = appointment.appointment_id
            elif queue_entry.target_branch:
                clinic_name = str(queue_entry.target_branch.name)
                clinic_address = str(queue_entry.target_branch.address or "")
        except Exception as e:
            print(f"⚠ Ошибка при получении данных о приеме: {e}")

        # Подготовка атрибутов
        attributes = {
            "patient_name": str(patient.get_full_name() or ""),
            "doctor_name": str(doctor_name or ""),
            "clinic_name": str(clinic_name or ""),
            "clinic_address": str(clinic_address or ""),
            "department_name": str(department_name or ""),
            "appointment_date": str(appointment_date or ""),
            "appointment_time": str(appointment_time or ""),
            "call_type": "queue",
            "gp": str(queue_reason_code or ""),
            "patient_id": str(patient.patient_code or ""),
            "queue_id": str(queue_entry.queue_id or "")
        }

        if appointment_id:
            attributes["reception_id"] = str(appointment_id)

        # Проверка на пустые или None значения
        for key in list(attributes.keys()):
            if attributes[key] is None or attributes[key] == "None" or attributes[key] == "null":
                attributes[key] = ""

            # Дополнительная обработка строк для удаления невидимых символов
            if isinstance(attributes[key], str):
                attributes[key] = attributes[key].strip()

        # Проверка типов данных
        print("Проверка типов данных в payload:")
        for key, value in attributes.items():
            print(f"  {key}: {type(value).__name__} = {value}")

        # Подготовка данных в плоском JSON для отправки (без вложенной структуры attributes)
        json_data = {
            "phone": phone,
            # Добавляем все атрибуты как отдельные поля в корне JSON, а не внутри attributes
            "patient_name": attributes.get("patient_name", ""),
            "doctor_name": attributes.get("doctor_name", ""),
            "clinic_name": attributes.get("clinic_name", ""),
            "clinic_address": attributes.get("clinic_address", ""),
            "department_name": attributes.get("department_name", ""),
            "appointment_date": attributes.get("appointment_date", ""),
            "appointment_time": attributes.get("appointment_time", ""),
            "call_type": "queue",
            "gp": attributes.get("gp", ""),
            "patient_id": attributes.get("patient_id", ""),
            "queue_id": attributes.get("queue_id", "")
        }

        if appointment_id:
            json_data["reception_id"] = str(appointment_id)

        print("Отправка плоской структуры JSON:")
        print(json.dumps(json_data, indent=2, ensure_ascii=False))

        url = f"{ACS_BASE_URL}/api/v2/bpm/public/bp/{api_key}/add_orders"

        try:
            print(f"Отправка заказа в ACS для пациента {patient.patient_code} с причиной {queue_reason_code}")
            # Отправляем как плоскую JSON структуру
            headers = {'Content-Type': 'application/json'}
            response = requests.post(url, json=json_data, headers=headers)

            print(f"Ответ сервера: {response.status_code}")
            if response.text:
                print(f"Текст ответа: {response.text[:200]}...")  # Печатаем первые 200 символов

            if response.status_code == 200:
                # Улучшенная обработка ответа с учетом возможных разных форматов
                try:
                    result_data = response.json()
                    print(f"Структура ответа: {type(result_data).__name__}")

                    # Извлечение order_key в зависимости от формата ответа
                    order_key = None

                    # Проверка формата ответа и корректное извлечение order_key
                    if isinstance(result_data, dict):
                        if 'data' in result_data and phone in result_data.get('data', {}):
                            phone_data = result_data.get('data', {}).get(phone, {})
                            if isinstance(phone_data, dict) and 'order' in phone_data:
                                order_key = phone_data.get('order')
                        elif 'order' in result_data:
                            order_key = result_data.get('order')
                    elif isinstance(result_data, list):
                        for item in result_data:
                            if isinstance(item, dict) and 'order' in item:
                                order_key = item.get('order')
                                break

                    print(f"Извлеченный order_key: {order_key}")

                    if order_key:
                        try:
                            # Создание или обновление записи звонка
                            if appointment:
                                call, created = Call.objects.get_or_create(
                                    appointment=appointment,
                                    call_type="queue",
                                    defaults={
                                        "order_key": order_key,
                                        "queue_id": queue_entry.queue_id,
                                        "patient_code": patient.patient_code
                                    }
                                )
                            else:
                                call, created = Call.objects.get_or_create(
                                    queue_id=queue_entry.queue_id,
                                    patient_code=patient.patient_code,
                                    call_type="queue",
                                    defaults={"order_key": order_key}
                                )

                            if created:
                                print(f"✅ Создан звонок для {patient.get_full_name()} очередь {queue_entry.queue_id}")
                                success_count += 1
                            else:
                                if call.order_key != order_key:
                                    call.order_key = order_key
                                    call.save(update_fields=['order_key'])
                                    print(f"🔄 Обновлен order_key для {patient.get_full_name()}")
                                success_count += 1
                        except Exception as e:
                            print(f"❌ Ошибка при создании записи звонка: {e}")
                            traceback.print_exc()
                            error_count += 1
                    else:
                        print(f"❌ Нет order_key в ответе для телефона {phone}")
                        error_count += 1

                except Exception as e:
                    print(f"❌ Ошибка при разборе ответа: {e}")
                    traceback.print_exc()
                    error_count += 1
            else:
                print(f"❌ Ошибка при отправке в ACS: {response.status_code} - {response.text}")
                error_count += 1

        except Exception as e:
            print(f"❌ Ошибка запроса ACS для пациента {patient.patient_code}: {e}")
            traceback.print_exc()
            error_count += 1

    print(f"Обработка завершена. Успешно: {success_count}, Ошибок: {error_count}")
    return success_count > 0


if __name__ == "__main__":
    process_queue_to_acs()