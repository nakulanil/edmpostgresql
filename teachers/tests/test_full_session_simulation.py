"""Multi-date booking-to-deadline simulation using PostgreSQL test fixtures.

Models a college session with pre-booked shifts and two staggered deadlines;
checks slot capacity, teacher quotas, assignment rules and idempotency.
"""
from collections import Counter
from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from teachers.models import Booking, ExamDate, Session, Slot, Teacher
from teachers.scheduler import auto_assign_duties


class MultiDateSessionSimulationTest(TestCase):
    def test_manual_bookings_then_staggered_automatic_assignment(self):
        session = Session.objects.create(
            name="Multi-date college simulation", status="active",
            semester_type="odd",
        )
        now = timezone.now()
        deadlines = [now + timedelta(hours=1), now + timedelta(hours=3)]
        dates = []
        slots = []
        for i, deadline in enumerate(deadlines):
            exam = ExamDate.objects.create(
                session=session,
                date=(now + timedelta(days=3 + i)).date(),
                booking_deadline=deadline,
                morning_start=time(9, 0), morning_end=time(12, 0),
                evening_start=time(14, 0), evening_end=time(17, 0),
            )
            dates.append(exam)
            for shift in ("Morning", "Evening"):
                slots.append(Slot.objects.create(
                    exam_date=exam, shift=shift, total_positions=4,
                ))

        teachers = []
        for i in range(12):
            user = User.objects.create_user(username=f"simulation-{i}")
            teachers.append(Teacher.objects.create(
                user=user, faculty_id=f"SIM-{i:03}",
                full_name=f"Simulation Teacher {i}",
                role="permanent", final_duties=2,
            ))

        # Teachers have booked some duties before the first deadline.
        for teacher, slot in zip(teachers[:3], slots[:3]):
            Booking.objects.create(teacher=teacher, slot=slot)
            slot.filled_positions += 1
            slot.save(update_fields=["filled_positions"])
            teacher.final_duties -= 1
            teacher.save(update_fields=["final_duties"])

        initial_bookings = Booking.objects.count()
        self.assertEqual(initial_bookings, 3)

        with patch("teachers.scheduler.timezone.now", return_value=deadlines[0] + timedelta(seconds=1)):
            auto_assign_duties(session_id=session.pk, exam_date_id=dates[0].pk)

        session.refresh_from_db()
        self.assertEqual(session.status, "active")
        self.assertEqual(Booking.objects.filter(slot__exam_date=dates[0]).count(), 8)
        self.assertEqual(Booking.objects.filter(slot__exam_date=dates[1]).count(), 1)

        with patch("teachers.scheduler.timezone.now", return_value=deadlines[1] + timedelta(seconds=1)):
            auto_assign_duties(session_id=session.pk, exam_date_id=dates[1].pk)
            # Retrying an already satisfied deadline cannot duplicate bookings.
            auto_assign_duties(session_id=session.pk, exam_date_id=dates[1].pk)

        self.assertEqual(Booking.objects.filter(slot__exam_date__session=session).count(), 16)
        self.assertEqual(
            Booking.objects.filter(slot__exam_date__session=session, auto_assigned=True).count(),
            13,
        )
        for slot in slots:
            slot.refresh_from_db()
            self.assertEqual(slot.filled_positions, 4)
            self.assertEqual(Booking.objects.filter(slot=slot).count(), 4)

        day_and_shift = Counter()
        for booking in Booking.objects.filter(slot__exam_date__session=session).select_related("slot__exam_date"):
            key = (booking.teacher_id, booking.slot.exam_date.date)
            day_and_shift[key] += 1
            self.assertLessEqual(day_and_shift[key], 2)
            self.assertEqual(
                Booking.objects.filter(
                    teacher_id=booking.teacher_id,
                    slot__exam_date=booking.slot.exam_date,
                    slot__shift=booking.slot.shift,
                ).count(),
                1,
            )

        for teacher in teachers:
            teacher.refresh_from_db()
            count = Booking.objects.filter(teacher=teacher, slot__exam_date__session=session).count()
            self.assertEqual(teacher.final_duties, 2 - count)
            self.assertGreaterEqual(teacher.final_duties, 0)

        session.refresh_from_db()
        self.assertEqual(session.status, "completed")
