import os
import django
import re

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.models import QueueReason, QueueReasonMapping
import logging


logger = logging.getLogger(__name__)


REASON_MAPPINGS = [
    # ADDID -> internal_code
    {'add_id': 4, 'internal_code': '00PP0consulta', 'internal_name': 'Консультация'},
    {'add_id': 5, 'internal_code': '00PP0prodolzhenie', 'internal_name': 'Лечение продолжить'},
    {'add_id': 1, 'internal_code': '0PP0profilac', 'internal_name': 'Программа профилактики'},
    {'add_id': 8, 'internal_code': '0PP0profilac', 'internal_name': 'Программа профилактики'}, # Диспансеризация
    {'add_id': 6, 'internal_code': '00PP0consulta', 'internal_name': 'Консультация'}, # Консультация
    {'add_id': 111113, 'internal_code': '0PP0adapta', 'internal_name': 'Адаптационный визит'}, # Пример из API
    # Добавьте остальные сопоставления на основе вашего опыта или документации
]


def create_reason_mappings():
    """
    Создает сопоставления между причинами из Инфоклиники и внутренними кодами.
    """
    # Определение сопоставлений на основе полученных данных из API
    REASON_MAPPINGS = [
        # Сопоставления подтвержденные из API
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
        {'add_id': 111113, 'internal_code': '0PP0adapta', 'internal_name': 'Адаптационный визит'}  # Из примера API
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


create_reason_mappings()