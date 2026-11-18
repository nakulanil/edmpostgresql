from django.apps import AppConfig
import logging
import sys
import os

logger = logging.getLogger(__name__)


class TeachersConfig(AppConfig):
    name = 'teachers'

def ready(self):
        # Skip scheduler startup during automated tests.
        # TESTING=1 must be set in the environment BEFORE Django initializes.
        if os.environ.get('TESTING') == '1':
            return

        # Only start scheduler if explicitly enabled via environment variable.
        # This prevents APScheduler from starting during management commands
        # (migrate, check, shell, dbshell, makemigrations) before database
        # tables exist. The dedicated scheduler service/web server should set
        # SCHEDULER_ENABLED=true to start the scheduler.
        if os.environ.get('SCHEDULER_ENABLED') != 'true':
            return

        # RUN_MAIN check sirf 'runserver' ke autoreloader duplicate-start
        # se bachne ke liye zaroori hai (autoreloader parent + child process
        # dono spawn karta hai). Gunicorn/production mein RUN_MAIN set hi
        # nahi hota, isliye us case mein ye check skip kar dena chahiye,
        # warna scheduler kabhi start hi nahi hota.
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        if getattr(self, '_scheduler_started', False):
            return
        TeachersConfig._scheduler_started = True

        try:
            from . import scheduler
            scheduler.start()
            logger.warning("Scheduler started from apps.py ready().")
        except Exception as e:
            logger.exception("Scheduler failed to start: %s", e)