"""Real PostgreSQL/APScheduler smoke test for future auto-assignment.

Uses TransactionTestCase so the scheduler worker's separate database connection
can see committed fixtures. This executes the actual dedicated scheduler command,
not the mocked scheduler used by test_future_deadline_scheduler.py.
"""

import threading
import time
from datetime import time as clock_time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from teachers.models import Booking, ExamDate, Session, Slot, Teacher


class RealFutureDeadlineSchedulerTest(TransactionTestCase):
    def test_scheduler_fires_persisted_job_after_deadline(self):
        if connection.vendor != "postgresql":
            self.skipTest("Integration test requires PostgreSQL")

        session = Session.objects.create(
            name="Real scheduler integration", status="active", semester_type="odd"
        )
        teacher = Teacher.objects.create(
            user=User.objects.create_user(username="real-scheduler-teacher"),
            faculty_id="REAL_SCHED_001",
            full_name="Real Scheduler Teacher",
            role="permanent",
            final_duties=1,
        )
        deadline = timezone.now() + timedelta(seconds=3)
        exam = ExamDate.objects.create(
            session=session,
            date=(timezone.now() + timedelta(days=2)).date(),
            booking_deadline=deadline,
            morning_start=clock_time(9, 0),
            morning_end=clock_time(12, 0),
            evening_start=clock_time(14, 0),
            evening_end=clock_time(17, 0),
        )
        slot = Slot.objects.create(
            exam_date=exam, shift="Morning", total_positions=1
        )
        timeout_at = time.monotonic() + 15
        pause = threading.Event()

        def poll_until_assigned(_seconds):
            if Booking.objects.filter(slot=slot).exists():
                raise KeyboardInterrupt
            if time.monotonic() >= timeout_at:
                raise AssertionError("Scheduler did not assign duty within 15 seconds")
            pause.wait(0.1)

        # The management command normally stays up forever. Replace only its
        # main-loop sleep to stop it after the real worker executes the job.
        # signal.signal is mocked to avoid altering the test runner's handlers.
        with (
            patch("teachers.management.commands.run_scheduler.signal.signal"),
            patch("time.sleep", side_effect=poll_until_assigned),
        ):
            call_command("run_scheduler", verbosity=0)

        self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)
        slot.refresh_from_db()
        teacher.refresh_from_db()
        self.assertEqual(slot.filled_positions, 1)
        self.assertEqual(teacher.final_duties, 0)
