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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJob

from teachers.scheduler import (
    auto_assign_duties,
    find_sessions_needing_assignment,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the APScheduler background scheduler as a dedicated process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process overdue deadlines once without starting APScheduler.",
        )

    def handle(self, *args, **options):
        once = options.get("once", False)

        # One-shot reconciliation must not start a background scheduler. Its
        # DjangoJobStore.shutdown() closes the active Django DB connection,
        # breaking TestCase transactions and subsequent queries. The daemon
        # mode below is responsible for persisting future deadline jobs.
        if once:
            self._process_overdue()
            logger.info("One-shot overdue reconciliation complete.")
            return

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

        scheduler_instance.add_jobstore(DjangoJobStore(), "default")
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

        self._process_overdue()

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
                    # Schedule on THIS dedicated scheduler instance. The global
                    # scheduler in teachers.scheduler is not running here.
                    scheduler_instance.add_job(
                        auto_assign_duties,
                        trigger=DateTrigger(run_date=exam_date.booking_deadline),
                        id=job_id,
                        name=f"Auto assign session {session.id} exam {exam_date.id}",
                        replace_existing=True,
                        jobstore="default",
                        kwargs={"session_id": session.id, "exam_date_id": exam_date.id},
                    )

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
    def _process_overdue(self):
        """Reconcile overdue exam dates synchronously and idempotently."""
        for session_id, exam_date_ids in find_sessions_needing_assignment().items():
            for exam_date_id in exam_date_ids:
                try:
                    auto_assign_duties(session_id=session_id, exam_date_id=exam_date_id)
                except Exception:
                    logger.exception(
                        "Auto-assignment failed for session %s, exam %s",
                        session_id, exam_date_id,
                    )
