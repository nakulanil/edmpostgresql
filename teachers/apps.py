from django.apps import AppConfig


class TeachersConfig(AppConfig):
    name = 'teachers'

    # APScheduler must only run in the dedicated `run_scheduler` process.
    # Never start a scheduler from AppConfig.ready(): web workers, management
    # commands and autoreloaders can each initialize this app independently.
