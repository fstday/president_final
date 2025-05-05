import os
import django
import pandas as pd
from tabulate import tabulate

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

# Импорт необходимых моделей
from reminder.models import Patient, Doctor, Clinic, Department, Appointment, QueueInfo


def extract_test_data():
    """Извлекает полные данные о пациентах, врачах, клиниках и отделениях для тестирования"""

    # 1. Получаем список всех уникальных пациентов с активными записями
    patients_with_appointments = Patient.objects.filter(
        appointments__is_active=True
    ).distinct()

    print("\n" + "=" * 80)
    print("ДАННЫЕ ДЛЯ ТЕСТИРОВАНИЯ ЗАПРОСА DOCT_SCHEDULE_FREE")
    print("=" * 80)

    # 2. Для каждого пациента выводим полную информацию
    for patient in patients_with_appointments:
        print(f"\n{'=' * 80}")
        print(f"ПАЦИЕНТ: {patient.full_name} (ID: {patient.patient_code})")
        print(f"{'=' * 80}")

        # Получаем активные записи
        appointments = Appointment.objects.filter(
            patient=patient,
            is_active=True
        ).select_related('doctor', 'clinic', 'department')

        if appointments:
            print("\nАКТИВНЫЕ ЗАПИСИ К ВРАЧАМ:")
            print("-" * 80)

            appointment_data = []
            for app in appointments:
                # Информация о враче
                doctor_id = "—"
                doctor_name = "—"
                if app.doctor:
                    doctor_id = app.doctor.doctor_code
                    doctor_name = app.doctor.full_name

                # Информация о клинике
                clinic_id = "—"
                clinic_name = "—"
                if app.clinic:
                    clinic_id = app.clinic.clinic_id
                    clinic_name = app.clinic.name

                # Информация об отделении
                dept_id = "—"
                dept_name = "—"
                if app.department:
                    dept_id = app.department.department_id
                    dept_name = app.department.name

                appointment_data.append({
                    'ID записи': app.appointment_id,
                    'Дата приема': app.start_time.strftime("%d.%m.%Y %H:%M"),
                    'ID врача': doctor_id,
                    'Врач': doctor_name,
                    'ID клиники': clinic_id,
                    'Клиника': clinic_name,
                    'ID отделения': dept_id,
                    'Отделение': dept_name,
                })

            df = pd.DataFrame(appointment_data)
            print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))
        else:
            print("\nНет активных записей к врачам")

        # Получаем очереди
        queues = QueueInfo.objects.filter(
            patient=patient
        ).select_related('branch', 'target_branch')

        if queues:
            print("\nОЧЕРЕДИ ПАЦИЕНТА:")
            print("-" * 80)

            queue_data = []
            for queue in queues:
                # Получаем код и имя врача из QueueInfo
                doctor_id = queue.doctor_code or "—"
                doctor_name = queue.doctor_name or "—"

                # Получаем данные по филиалам
                source_clinic_id = "—"
                source_clinic_name = "—"
                if queue.branch:
                    source_clinic_id = queue.branch.clinic_id
                    source_clinic_name = queue.branch.name

                target_clinic_id = "—"
                target_clinic_name = "—"
                if queue.target_branch:
                    target_clinic_id = queue.target_branch.clinic_id
                    target_clinic_name = queue.target_branch.name

                # Отделение (может быть в department_number/name или связано с врачом)
                dept_id = queue.department_number or "—"
                dept_name = queue.department_name or "—"

                queue_data.append({
                    'ID очереди': queue.queue_id,
                    'ID врача': doctor_id,
                    'Врач': doctor_name,
                    'ID клиники (исх.)': source_clinic_id,
                    'Клиника (исх.)': source_clinic_name,
                    'ID клиники (цел.)': target_clinic_id,
                    'Клиника (цел.)': target_clinic_name,
                    'ID отделения': dept_id,
                    'Отделение': dept_name,
                })

            df = pd.DataFrame(queue_data)
            print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))
        else:
            print("\nНет очередей")

    # 3. Выводим данные о всех врачах с их отделениями и клиниками
    print("\n\n" + "=" * 80)
    print("СПИСОК ВРАЧЕЙ С ОТДЕЛЕНИЯМИ И КЛИНИКАМИ")
    print("=" * 80)

    doctors = Doctor.objects.select_related('department', 'clinic').all()

    doctor_data = []
    for doctor in doctors:
        # Информация о клинике
        clinic_id = "—"
        clinic_name = "—"
        if doctor.clinic:
            clinic_id = doctor.clinic.clinic_id
            clinic_name = doctor.clinic.name

        # Информация об отделении
        dept_id = "—"
        dept_name = "—"
        if doctor.department:
            dept_id = doctor.department.department_id
            dept_name = doctor.department.name

        doctor_data.append({
            'ID врача': doctor.doctor_code,
            'ФИО врача': doctor.full_name,
            'ID клиники': clinic_id,
            'Клиника': clinic_name,
            'ID отделения': dept_id,
            'Отделение': dept_name,
        })

    df = pd.DataFrame(doctor_data)
    print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))

    # 4. Выводим данные о всех клиниках и их отделениях
    print("\n\n" + "=" * 80)
    print("СПИСОК КЛИНИК И ИХ ОТДЕЛЕНИЙ")
    print("=" * 80)

    clinics = Clinic.objects.prefetch_related('departments').all()

    for clinic in clinics:
        print(f"\nКЛИНИКА: {clinic.name} (ID: {clinic.clinic_id})")
        print("-" * 80)

        departments = clinic.departments.all()
        if departments:
            dept_data = []
            for dept in departments:
                dept_data.append({
                    'ID отделения': dept.department_id,
                    'Название отделения': dept.name,
                })

            df = pd.DataFrame(dept_data)
            print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))
        else:
            print("Нет связанных отделений")


if __name__ == "__main__":
    extract_test_data()
