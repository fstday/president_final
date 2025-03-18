import os
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from openai import OpenAI

from reminder.models import Assistant
from reminder.openai_assistant.assistant_tools import TOOLS

logger = logging.getLogger(__name__)

# Import the improved instructions from our updated file
with open(os.path.join(os.path.dirname(__file__), '..', '..', 'openai_assistant', 'assistant_instructions.md'), 'r',
          encoding='utf-8') as f:
    DEFAULT_INSTRUCTIONS = f.read()


class Command(BaseCommand):
    help = 'Create or update an OpenAI assistant and save it to the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            type=str,
            default='Медицинский ассистент',
            help='Name for the assistant'
        )

        parser.add_argument(
            '--model',
            type=str,
            default='gpt-4-mini',
            help='OpenAI model to use (gpt-4, gpt-4-mini, gpt-3.5-turbo)'
        )

        parser.add_argument(
            '--instructions_file',
            type=str,
            help='Path to file with instructions for the assistant (optional)'
        )

        parser.add_argument(
            '--update_existing',
            action='store_true',
            help='Update existing assistant if it exists'
        )

    def handle(self, *args, **options):
        try:
            name = options['name']
            model = options['model']
            instructions_file = options.get('instructions_file')
            update_existing = options.get('update_existing', False)

            # Load instructions from file if specified
            instructions = DEFAULT_INSTRUCTIONS
            if instructions_file and os.path.exists(instructions_file):
                with open(instructions_file, 'r', encoding='utf-8') as f:
                    instructions = f.read()

            # Special emphasis on function calling
            function_calling_instructions = """
ВАЖНО: Всегда используйте функции вместо текстовых ответов в следующих случаях:

1. Когда пользователь спрашивает о свободных окошках или времени (пример: "Какие свободные окошки на сегодня") - 
   ВСЕГДА вызывайте функцию which_time_in_certain_day с параметрами reception_id и date_time

2. Когда пользователь интересуется своей текущей записью - 
   ВСЕГДА вызывайте функцию appointment_time_for_patient с параметром patient_code

3. Когда пользователь хочет записаться или перенести запись - 
   ВСЕГДА вызывайте функцию reserve_reception_for_patient

4. Когда пользователь хочет отменить запись - 
   ВСЕГДА вызывайте функцию delete_reception_for_patient

НИКОГДА не отвечайте текстом в этих ситуациях. Вместо этого ВСЕГДА вызывайте соответствующую функцию.
"""

            # Add function calling emphasis to instructions
            instructions = function_calling_instructions + "\n\n" + instructions

            # Check API key
            api_key = settings.OPEN_AI_API_KEY
            if not api_key:
                self.stderr.write(
                    self.style.ERROR('OpenAI API key not found. Set OPEN_AI_API_KEY in settings.')
                )
                return

            # Create OpenAI client
            client = OpenAI(api_key=api_key)

            # Check if assistant with this name already exists in DB
            existing_assistant = Assistant.objects.filter(name=name).first()

            if existing_assistant and not update_existing:
                self.stderr.write(
                    self.style.WARNING(f'Assistant with name "{name}" already exists. '
                                       f'Use --update_existing to update.')
                )
                return

            # Get all assistants from OpenAI
            assistants = client.beta.assistants.list(limit=100)
            openai_assistant = None

            # Check if assistant exists in OpenAI
            for assistant in assistants.data:
                if assistant.name == name:
                    openai_assistant = assistant
                    break

            # Create or update assistant in OpenAI
            if openai_assistant:
                self.stdout.write(self.style.SUCCESS(f'Updating existing OpenAI assistant "{name}"'))
                assistant_info = client.beta.assistants.update(
                    assistant_id=openai_assistant.id,
                    name=name,
                    instructions=instructions,
                    model=model,
                    tools=TOOLS
                )
            else:
                self.stdout.write(self.style.SUCCESS(f'Creating new OpenAI assistant "{name}"'))
                assistant_info = client.beta.assistants.create(
                    name=name,
                    instructions=instructions,
                    model=model,
                    tools=TOOLS
                )

            # Save or update assistant in DB
            if existing_assistant:
                existing_assistant.assistant_id = assistant_info.id
                existing_assistant.model = model
                existing_assistant.instructions = instructions
                existing_assistant.save()
                self.stdout.write(self.style.SUCCESS(f'Assistant "{name}" successfully updated in OpenAI and DB'))
            else:
                assistant = Assistant.objects.create(
                    assistant_id=assistant_info.id,
                    name=name,
                    model=model,
                    instructions=instructions
                )
                self.stdout.write(
                    self.style.SUCCESS(f'Assistant "{name}" successfully created in OpenAI and saved to DB'))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Error: {str(e)}'))
            logger.exception("Error creating assistant")
