"""Concurrent booking safety against real PostgreSQL row locks.

Transactions run in separate threads/connections to exercise the same
contention as independent web requests. No production database is used.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from teachers.models import Booking, ExamDate, Session, Slot, Teacher


class ConcurrentBookingTest(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL SELECT FOR UPDATE")
        self.session = Session.objects.create(
            name="Parallel booking", status="active", semester_type="odd"
        )
        self.exam = ExamDate.objects.create(
            session=self.session,
            date=timezone.localdate() + timedelta(days=4),
            booking_deadline=timezone.now() + timedelta(days=2),
            morning_start=time(9, 0), morning_end=time(12, 0),
            evening_start=time(14, 0), evening_end=time(17, 0),
        )

    def make_teacher(self, i, duties):
        user = User.objects.create_user(username=f"concurrent-{i}", password="testpass")
        teacher = Teacher.objects.create(
            user=user, faculty_id=f"CONCURRENT-{i}",
            full_name=f"Teacher {i}", role="permanent", final_duties=duties,
        )
        return user, teacher

    def book_in_parallel(self, requests):
        gate = Barrier(len(requests))

        def run(user, slot_id):
            close_old_connections()
            try:
                client = Client()
                client.force_login(user)
                gate.wait(timeout=10)
                response = client.post(reverse("book_slot", args=[slot_id]))
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            futures = [pool.submit(run, user, slot_id) for user, slot_id in requests]
            return [future.result(timeout=15) for future in futures]

    def test_same_teacher_two_slots_with_one_duty_left(self):
        user, teacher = self.make_teacher(1, duties=1)
        first = Slot.objects.create(exam_date=self.exam, shift="Morning", total_positions=2)
        second = Slot.objects.create(exam_date=self.exam, shift="Evening", total_positions=2)
        statuses = self.book_in_parallel([(user, first.pk), (user, second.pk)])
        self.assertEqual(statuses, [302, 302])
        self.assertEqual(Booking.objects.filter(teacher=teacher).count(), 1)
        teacher.refresh_from_db()
        self.assertEqual(teacher.final_duties, 0)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.filled_positions + second.filled_positions, 1)

    def test_two_teachers_compete_for_last_position(self):
        user_a, teacher_a = self.make_teacher(2, duties=1)
        user_b, teacher_b = self.make_teacher(3, duties=1)
        slot = Slot.objects.create(
            exam_date=self.exam, shift="Morning", total_positions=1,
        )
        statuses = self.book_in_parallel([(user_a, slot.pk), (user_b, slot.pk)])
        self.assertEqual(statuses, [302, 302])
        self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)
        slot.refresh_from_db()
        self.assertEqual(slot.filled_positions, 1)
        teacher_a.refresh_from_db()
        teacher_b.refresh_from_db()
        self.assertEqual(teacher_a.final_duties + teacher_b.final_duties, 1)

    def test_eight_teachers_compete_for_three_positions(self):
        """Contention must not overfill a slot or lose teacher counters."""
        participants = [self.make_teacher(i, duties=1) for i in range(10, 18)]
        slot = Slot.objects.create(
            exam_date=self.exam, shift="Morning", total_positions=3,
        )
        statuses = self.book_in_parallel(
            [(user, slot.pk) for user, _teacher in participants]
        )
        self.assertEqual(statuses, [302] * len(participants))
        bookings = Booking.objects.filter(slot=slot)
        self.assertEqual(bookings.count(), 3)
        self.assertEqual(bookings.values("teacher_id").distinct().count(), 3)
        slot.refresh_from_db()
        self.assertEqual(slot.filled_positions, 3)
        remaining = 0
        for _user, teacher in participants:
            teacher.refresh_from_db()
            self.assertIn(teacher.final_duties, (0, 1))
            self.assertEqual(
                Booking.objects.filter(slot=slot, teacher=teacher).count(),
                1 - teacher.final_duties,
            )
            remaining += teacher.final_duties
        self.assertEqual(remaining, 5)
