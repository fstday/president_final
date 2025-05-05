import json
import redis
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class ConversationContextManager:
    """Manages conversation context using Redis."""

    def __init__(self):
        """Initialize Redis connection."""
        try:
            logger.info(f"Connecting to Redis: {settings.REDIS_HOST}:{settings.REDIS_PORT}, DB: {settings.REDIS_DB}")
            self.redis = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=True  # Automatically decode responses to strings
            )
            # Test connection
            self.redis.ping()
            logger.info("Successfully connected to Redis")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis = None

    def get_context_key(self, patient_code):
        """Generate a unique key for patient's context."""
        return f"context:patient:{patient_code}"

    def save_context(self, patient_code, context_data):
        """Save conversation context to Redis."""
        if not self.redis:
            logger.warning("Redis not available, context not saved")
            return False

        try:
            key = self.get_context_key(patient_code)
            serialized_data = json.dumps(context_data)
            self.redis.set(key, serialized_data, ex=settings.REDIS_CONTEXT_EXPIRY)
            logger.info(f"Context saved for patient {patient_code}")
            return True
        except Exception as e:
            logger.error(f"Error saving context: {e}")
            return False

    def get_context(self, patient_code):
        """Retrieve conversation context from Redis."""
        if not self.redis:
            logger.warning("Redis not available, context not retrieved")
            return None

        try:
            key = self.get_context_key(patient_code)
            serialized_data = self.redis.get(key)

            if not serialized_data:
                logger.info(f"No context found for patient {patient_code}")
                return None

            context_data = json.loads(serialized_data)
            logger.info(f"Retrieved context for patient {patient_code}")
            return context_data
        except Exception as e:
            logger.error(f"Error retrieving context: {e}")
            return None

    def update_context(self, patient_code, updates):
        """Update existing context with new data."""
        existing_context = self.get_context(patient_code) or {}
        existing_context.update(updates)
        return self.save_context(patient_code, existing_context)

    def delete_context(self, patient_code):
        """Delete context for a patient."""
        if not self.redis:
            return False

        try:
            key = self.get_context_key(patient_code)
            self.redis.delete(key)
            logger.info(f"Deleted context for patient {patient_code}")
            return True
        except Exception as e:
            logger.error(f"Error deleting context: {e}")
            return False
