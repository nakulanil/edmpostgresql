"""Regression coverage for the dedicated scheduler's future-deadline jobs.

The background scheduler is mocked so tests can check job registration and
invoke the registered callback deterministically without sleeping or starting
threads inside Django's transactional TestCase.
"""

from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teachers.models import Booking, ExamDate, Session, Slot, Teacher
from teachers.scheduler import auto_assign_duties


class FutureDeadlineSchedulingTest(TestCase):
    def setUp(self):
        self.session = Session.objects.create(
            name="Future scheduling test", status="active", semester_type="odd"
        )
        self.user = User.objects.create_user(
            username="future-scheduler-teacher", password="test-password"
        )
        self.teacher = Teacher.objects.create(
            user=self.user,
            faculty_id="FUTURE001",
            full_name="Future Scheduler Teacher",
            role="permanent",
            final_duties=2,
        )
        self.deadline = timezone.now() + timedelta(hours=2)
        self.exam = ExamDate.objects.create(
            session=self.session,
            date=(timezone.now() + timedelta(days=3)).date(),
            booking_deadline=self.deadline,
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        self.slot = Slot.objects.create(
            exam_date=self.exam, shift="Morning", total_positions=1
        )

    def _run_daemon_reconciliation(self, existing_job=None):
        # Avoid real threads and signal-handler changes while preserving the
        # management command's actual database queries and scheduling decisions.
        with (
            patch(
                "teachers.management.commands.run_scheduler.BackgroundScheduler"
            ) as scheduler_cls,
            patch("teachers.management.commands.run_scheduler.signal.signal"),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            scheduler = scheduler_cls.return_value
            scheduler.running = True
            scheduler.get_job.return_value = existing_job
            call_command("run_scheduler", verbosity=0)
            return scheduler

    def test_future_deadline_registers_on_dedicated_scheduler_and_executes(self):
        scheduler = self._run_daemon_reconciliation()

        self.assertEqual(scheduler.add_job.call_count, 1)
        args, kwargs = scheduler.add_job.call_args
        self.assertIs(args[0], auto_assign_duties)
        self.assertEqual(
            kwargs["id"],
            f"auto_assign_duties_{self.session.pk}_{self.exam.pk}",
        )
        self.assertEqual(
            kwargs["kwargs"],
            {"session_id": self.session.pk, "exam_date_id": self.exam.pk},
        )
        self.assertEqual(kwargs["jobstore"], "default")
        self.assertEqual(kwargs["trigger"].run_date, self.deadline)

        # Merely scheduling a future job cannot allocate duties early.
        self.assertFalse(Booking.objects.filter(slot=self.slot).exists())

        # Emulate APScheduler firing the registered callback just after expiry.
        with patch(
            "teachers.scheduler.timezone.now",
            return_value=self.deadline + timedelta(seconds=1),
        ):
            args[0](**kwargs["kwargs"])

        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 1)
        self.slot.refresh_from_db()
        self.teacher.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, 1)
        self.assertEqual(self.teacher.final_duties, 1)

    def test_existing_future_job_is_not_registered_twice(self):
        scheduler = self._run_daemon_reconciliation(existing_job=object())
        scheduler.add_job.assert_not_called()
