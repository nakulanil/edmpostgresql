"""
Phase 3 Batch 1: Reconciliation function tests.
Tests for find_sessions_needing_assignment() in teachers.scheduler.
"""

from django.test import TestCase
from django.utils import timezone
from datetime import timedelta, time
from unittest.mock import patch

from teachers.models import Session, ExamDate, Slot, Teacher, Department
from teachers.scheduler import find_sessions_needing_assignment
from django.contrib.auth.models import User


class ReconciliationFunctionTest(TestCase):
    """Tests for find_sessions_needing_assignment() read-only reconciliation function."""

    def setUp(self):
        self.dept = Department.objects.create(name="CS")
        self.session = Session.objects.create(
            name="Test Session", status='active', semester_type='odd'
        )
        self.user = User.objects.create_user(username='t1', password='pass')
        self.teacher = Teacher.objects.create(
            user=self.user, faculty_id='T001', full_name='Teacher One',
            role='permanent', final_duties=3, department=self.dept,
        )

    def _make_exam_date(self, session, days_offset, deadline_offset_hours):
        return ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=days_offset),
            booking_deadline=timezone.now() + timedelta(hours=deadline_offset_hours),
            morning_start=time(9, 0), morning_end=time(12, 0),
            evening_start=time(14, 0), evening_end=time(17, 0),
            total_students=100,
        )

    def test_active_session_with_expired_deadline(self):
        exam = self._make_exam_date(self.session, days_offset=1, deadline_offset_hours=-2)
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertIn(self.session.id, result)
        self.assertEqual(result[self.session.id], [exam.id])

    def test_active_session_with_only_future_deadlines(self):
        exam = self._make_exam_date(self.session, days_offset=5, deadline_offset_hours=24)
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertNotIn(self.session.id, result)

    def test_session_with_mixed_deadlines(self):
        expired_exam = self._make_exam_date(self.session, days_offset=1, deadline_offset_hours=-2)
        future_exam = self._make_exam_date(self.session, days_offset=5, deadline_offset_hours=24)
        Slot.objects.create(exam_date=expired_exam, shift='Morning', total_positions=5)
        Slot.objects.create(exam_date=future_exam, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertIn(self.session.id, result)
        self.assertEqual(result[self.session.id], [expired_exam.id])
        self.assertNotIn(future_exam.id, result[self.session.id])

    def test_completed_session_excluded(self):
        self.session.status = 'completed'
        self.session.save()
        exam = self._make_exam_date(self.session, days_offset=1, deadline_offset_hours=-2)
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertNotIn(self.session.id, result)

    def test_upcoming_session_excluded(self):
        self.session.status = 'upcoming'
        self.session.save()
        exam = self._make_exam_date(self.session, days_offset=1, deadline_offset_hours=-2)
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertNotIn(self.session.id, result)

    def test_session_without_exam_dates_excluded(self):
        empty_session = Session.objects.create(name="Empty", status='active', semester_type='odd')
        result = find_sessions_needing_assignment()
        self.assertNotIn(empty_session.id, result)

    def test_multiple_active_sessions(self):
        session2 = Session.objects.create(name="Session 2", status='active', semester_type='odd')
        exam1 = self._make_exam_date(self.session, days_offset=1, deadline_offset_hours=-2)
        exam2 = self._make_exam_date(session2, days_offset=5, deadline_offset_hours=24)
        Slot.objects.create(exam_date=exam1, shift='Morning', total_positions=5)
        Slot.objects.create(exam_date=exam2, shift='Morning', total_positions=5)
        result = find_sessions_needing_assignment()
        self.assertIn(self.session.id, result)
        self.assertEqual(result[self.session.id], [exam1.id])
        self.assertNotIn(session2.id, result)

    def test_boundary_at_exact_deadline(self):
        fixed_now = timezone.now()
        exam_date_value = fixed_now.date() + timedelta(days=1)
        exam = ExamDate.objects.create(
            session=self.session,
            date=exam_date_value,
            booking_deadline=fixed_now,
            morning_start=time(9, 0), morning_end=time(12, 0),
            evening_start=time(14, 0), evening_end=time(17, 0),
            total_students=100,
        )
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        with patch('django.utils.timezone.now', return_value=fixed_now):
            result = find_sessions_needing_assignment()
        self.assertNotIn(self.session.id, result)

    def test_boundary_immediately_after_deadline(self):
        fixed_now = timezone.now()
        past_deadline = fixed_now - timedelta(seconds=1)
        exam_date_value = fixed_now.date() + timedelta(days=1)
        exam = ExamDate.objects.create(
            session=self.session,
            date=exam_date_value,
            booking_deadline=past_deadline,
            morning_start=time(9, 0), morning_end=time(12, 0),
            evening_start=time(14, 0), evening_end=time(17, 0),
            total_students=100,
        )
        Slot.objects.create(exam_date=exam, shift='Morning', total_positions=5)
        with patch('django.utils.timezone.now', return_value=fixed_now):
            result = find_sessions_needing_assignment()
        self.assertIn(self.session.id, result)
        self.assertEqual(result[self.session.id], [exam.id])
