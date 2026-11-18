"""Tests for reconciliation of Excel-imported and edited exam deadlines."""

from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from teachers.management.commands.run_scheduler import Command
from teachers.models import ExamDate, Session


class ExcelDeadlineReconciliationTest(TestCase):
    def setUp(self):
        self.session = Session.objects.create(
            name="Uploaded session", status="active", semester_type="odd"
        )
        self.deadline = timezone.now() + timedelta(hours=3)
        self.exam = ExamDate.objects.create(
            session=self.session,
            date=(timezone.now() + timedelta(days=4)).date(),
            booking_deadline=self.deadline,
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        self.scheduler = MagicMock()
        self.jobs = {}
        self.scheduler.get_job.side_effect = self.jobs.get
        self.scheduler.get_jobs.side_effect = lambda: list(self.jobs.values())

        def add_job(func, **kwargs):
            self.jobs[kwargs["id"]] = SimpleNamespace(
                id=kwargs["id"], next_run_time=kwargs["trigger"].run_date,
                func=func, kwargs=kwargs["kwargs"],
            )

        self.scheduler.add_job.side_effect = add_job
        self.scheduler.remove_job.side_effect = self.jobs.pop

    @property
    def job_id(self):
        return f"auto_assign_duties_{self.session.pk}_{self.exam.pk}"

    def sync(self):
        Command()._sync_future_jobs(self.scheduler)

    def test_new_excel_exam_date_is_scheduled_only_once(self):
        self.sync()
        self.assertIn(self.job_id, self.jobs)
        self.assertEqual(self.jobs[self.job_id].next_run_time, self.deadline)
        self.assertEqual(
            self.jobs[self.job_id].kwargs,
            {"session_id": self.session.pk, "exam_date_id": self.exam.pk},
        )
        self.sync()
        self.scheduler.add_job.assert_called_once()

    def test_changed_deadline_replaces_existing_job(self):
        self.sync()
        replacement = self.deadline + timedelta(hours=4)
        self.exam.booking_deadline = replacement
        self.exam.save(update_fields=["booking_deadline"])
        self.sync()
        self.assertEqual(self.scheduler.add_job.call_count, 2)
        self.assertEqual(self.jobs[self.job_id].next_run_time, replacement)

    def test_deleted_exam_date_removes_job(self):
        self.sync()
        self.exam.delete()
        self.sync()
        self.assertNotIn(self.job_id, self.jobs)
        self.scheduler.remove_job.assert_called_once_with(self.job_id)

    def test_inactive_session_removes_job(self):
        self.sync()
        self.session.status = "completed"
        self.session.save(update_fields=["status"])
        self.sync()
        self.assertNotIn(self.job_id, self.jobs)
        self.scheduler.remove_job.assert_called_once_with(self.job_id)
