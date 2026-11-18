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
        # Keep stored jobs paused until we reconcile the current DB state.
        scheduler_instance.start(paused=True)
        logger.info("Scheduler started (paused for reconciliation).")

        # Clean up legacy jobs (no session/exam_date scoping)
        try:
            legacy_jobs = DjangoJob.objects.filter(id="auto_assign_duties")
            if legacy_jobs.exists():
                legacy_jobs.delete()
                logger.info("Removed legacy auto_assign_duties job(s).")
        except Exception:
            logger.exception("Failed to clean up legacy jobs.")

        self._process_overdue()
        self._sync_future_jobs(scheduler_instance)
        scheduler_instance.resume()

        logger.info("Scheduler running. Press Ctrl+C to stop.")
        try:
            # Keep the main thread alive
            while True:
                import time as _time
                _time.sleep(10)
                self._process_overdue()
                self._sync_future_jobs(scheduler_instance)
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

    def _sync_future_jobs(self, scheduler_instance):
        """Reconcile the dedicated scheduler against committed ExamDate rows.

        Admin uploads do not create scheduler jobs themselves. Polling allows
        new dates, edited deadlines, and deleted dates to be reflected without
        restarting the scheduler or starting a scheduler in the web workers.
        """
        from teachers.models import ExamDate

        now = timezone.now()
        future_exams = ExamDate.objects.filter(
            session__status="active",
            booking_deadline__gt=now,
        ).select_related("session")

        desired_ids = set()
        for exam_date in future_exams:
            session_id = exam_date.session_id
            job_id = f"auto_assign_duties_{session_id}_{exam_date.pk}"
            desired_ids.add(job_id)
            job = scheduler_instance.get_job(job_id)
            if job is not None:
                current_run_time = getattr(job, "next_run_time", None)
                # An object without next_run_time is a test double or an
                # unknown job state; do not replace it blindly.
                if current_run_time is None or current_run_time == exam_date.booking_deadline:
                    continue

            scheduler_instance.add_job(
                auto_assign_duties,
                trigger=DateTrigger(run_date=exam_date.booking_deadline),
                id=job_id,
                name=f"Auto assign session {session_id} exam {exam_date.pk}",
                replace_existing=True,
                jobstore="default",
                kwargs={"session_id": session_id, "exam_date_id": exam_date.pk},
            )
            logger.info("Synced duty assignment job %s at %s", job_id, exam_date.booking_deadline)

        # Discard jobs for removed exams, inactive sessions, and old deadlines.
        for job in scheduler_instance.get_jobs():
            if job.id.startswith("auto_assign_duties_") and job.id not in desired_ids:
                scheduler_instance.remove_job(job.id)
                logger.info("Removed obsolete assignment job %s", job.id)
