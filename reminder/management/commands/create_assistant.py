import os
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from openai import OpenAI

from reminder.models import Assistant
from reminder.openai_assistant.assistant_tools import create_assistant_with_tools
from reminder.openai_assistant.assistant_instructions import DEFAULT_INSTRUCTIONS

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Создание или обновление OpenAI ассистента и сохранение его в БД'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            type=str,
            default='Медицинский ассистент',
            help='Имя для ассистента'
        )

        parser.add_argument(
            '--model',
            type=str,
            default='gpt-4-mini',
            help='Модель OpenAI для использования (gpt-4, gpt-4-mini, gpt-3.5-turbo)'
        )

        parser.add_argument(
            '--instructions_file',
            type=str,
            help='Путь к файлу с инструкциями для ассистента (опционально)'
        )

        parser.add_argument(
            '--update_existing',
            action='store_true',
            help='Обновить существующего ассистента, если он существует'
        )

    def handle(self, *args, **options):
        try:
            name = options['name']
            model = options['model']
            instructions_file = options.get('instructions_file')
            update_existing = options.get('update_existing', False)

            # Загружаем инструкции из файла, если указан
            instructions = DEFAULT_INSTRUCTIONS
            if instructions_file and os.path.exists(instructions_file):
                with open(instructions_file, 'r', encoding='utf-8') as f:
                    instructions = f.read()

            # Проверяем API ключ
            api_key = settings.OPEN_AI_API_KEY
            if not api_key:
                self.stderr.write(
                    self.style.ERROR('API ключ OpenAI не найден. Установите OPEN_AI_API_KEY в настройках.'))
                return

            # Создаем клиент OpenAI
            client = OpenAI(api_key=api_key)

            # Проверяем, существует ли уже ассистент с таким именем в БД
            existing_assistant = Assistant.objects.filter(name=name).first()

            if existing_assistant and not update_existing:
                self.stderr.write(
                    self.style.WARNING(f'Ассистент с именем "{name}" уже существует. '
                                       f'Используйте --update_existing для обновления.')
                )
                return

            # Создаем или обновляем ассистента в OpenAI
            assistant_info = create_assistant_with_tools(
                client=client,
                name=name,
                instructions=instructions,
                model=model
            )

            # Сохраняем или обновляем ассистента в БД
            if existing_assistant:
                existing_assistant.assistant_id = assistant_info.id
                existing_assistant.model = model
                existing_assistant.instructions = instructions
                existing_assistant.save()
                self.stdout.write(self.style.SUCCESS(f'Ассистент "{name}" успешно обновлен в OpenAI и БД'))
            else:
                assistant = Assistant.objects.create(
                    assistant_id=assistant_info.id,
                    name=name,
                    model=model,
                    instructions=instructions
                )
                self.stdout.write(self.style.SUCCESS(f'Ассистент "{name}" успешно создан в OpenAI и сохранен в БД'))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Ошибка: {str(e)}'))
            logger.exception("Ошибка при создании ассистента")
