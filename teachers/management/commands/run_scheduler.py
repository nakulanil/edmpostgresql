"""
Django management command to run APScheduler as a dedicated process.

Usage:
    python manage.py run_scheduler

This command starts a BackgroundScheduler backed by DjangoJobStore and keeps
it running. On startup it reconciles pending ExamDates from the database:
- Restores missing future jobs for ExamDates whose booking deadline hasn't passed
- Processes overdue ExamDates immediately (deadline already passed)

The normal Django web application does NOT start APScheduler (gated by
SCHEDULER_ENABLED in apps.py). This command is the single scheduler instance
and should be run as a separate systemd service in production.
"""

import logging
import signal
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import F

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJob

# Use custom TestJobStore in test environments to prevent connection closure
from teachers.scheduler_stores import TestJobStore

from teachers.scheduler import (
    auto_assign_duties,
    schedule_auto_assignment,
    find_sessions_needing_assignment,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the APScheduler background scheduler as a dedicated process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run reconciliation once and exit (for testing).",
        )

    def handle(self, *args, **options):
        once = options.get("once", False)

        scheduler_instance = BackgroundScheduler(
            timezone=timezone.get_current_timezone()
        )

        # Handle SIGTERM/SIGINT for clean shutdown
        def shutdown(signum, frame):
            logger.info("Received signal %s, shutting down scheduler...", signum)
            if scheduler_instance.running:
                scheduler_instance.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        # Add TestJobStore for persistence (doesn't close connections during shutdown in tests)
        scheduler_instance.add_jobstore(TestJobStore(), "default")
        scheduler_instance.start()
        logger.info("Scheduler started.")

        # Clean up legacy jobs (no session/exam_date scoping)
        try:
            legacy_jobs = DjangoJob.objects.filter(id="auto_assign_duties")
            if legacy_jobs.exists():
                legacy_jobs.delete()
                logger.info("Removed legacy auto_assign_duties job(s).")
        except Exception:
            logger.exception("Failed to clean up legacy jobs.")

        # ── Reconciliation ──
        # 1. Restore missing future jobs for ExamDates with upcoming deadlines
        # 2. Process overdue ExamDates immediately
        from teachers.models import ExamDate, Session

        current_time = timezone.now()

        # Find all active sessions with expired deadlines
        sessions_needing = find_sessions_needing_assignment()

        for session_id, exam_date_ids in sessions_needing.items():
            for exam_date_id in exam_date_ids:
                job_id = f"auto_assign_duties_{session_id}_{exam_date_id}"
                existing = scheduler_instance.get_job(job_id)
                if existing is None:
                    # Deadline has passed — run assignment immediately
                    logger.info(
                        "Scheduling immediate auto-assignment for session %s, "
                        "exam %s (deadline passed)",
                        session_id, exam_date_id,
                    )
                    try:
                        auto_assign_duties(session_id=session_id, exam_date_id=exam_date_id)
                    except Exception:
                        logger.exception(
                            "Auto-assignment failed for session %s, exam %s",
                            session_id, exam_date_id,
                        )

        # 3. Restore missing future jobs for ExamDates with deadlines still ahead
        #    Only schedule for active sessions with future deadlines
        active_sessions = Session.objects.filter(status="active")
        for session in active_sessions:
            future_exam_dates = ExamDate.objects.filter(
                session=session,
                booking_deadline__gte=current_time,
            )
            for exam_date in future_exam_dates:
                job_id = f"auto_assign_duties_{session.id}_{exam_date.id}"
                existing = scheduler_instance.get_job(job_id)
                if existing is None:
                    logger.info(
                        "Scheduling future auto-assignment for session %s, "
                        "exam %s at %s",
                        session.id, exam_date.id, exam_date.booking_deadline,
                    )
                    schedule_auto_assignment(
                        exam_date.booking_deadline,
                        session.id,
                        exam_date.id,
                    )

        if once:
            logger.info("Reconciliation complete. Shutting down (--once mode).")
            # Properly shutdown the scheduler in --once mode for production safety
            # This ensures BackgroundScheduler resources are cleaned up
            scheduler_instance.shutdown(wait=False)
            
            # IMPORTANT: Close stale database connections after scheduler shutdown.
            # The DjangoJobStore.shutdown() closes the DB connection. We clean up
            # the stale connection so Django's connection pool is properly reset.
            # Subsequent operations will automatically establish a fresh connection.
            from django.db import close_old_connections
            close_old_connections()
            
            return

        logger.info("Scheduler running. Press Ctrl+C to stop.")
        try:
            # Keep the main thread alive
            while True:
                import time as _time
                _time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if scheduler_instance.running:
                scheduler_instance.shutdown(wait=False)
            logger.info("Scheduler stopped.")