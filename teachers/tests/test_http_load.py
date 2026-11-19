"""HTTP-level booking load regression with 100 distinct faculty accounts.

This is a Django TransactionTestCase: it uses the isolated test database and
real PostgreSQL connections, not production. Requests hit the real booking
view via independent authenticated Django test clients. It is NOT a benchmark
of Nginx, Gunicorn, network latency, or the college VM.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import time, timedelta
from statistics import median
from time import perf_counter

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from teachers.models import Booking, ExamDate, Session, Slot, Teacher

logger = logging.getLogger(__name__)


class HundredFacultyHttpLoadTest(TransactionTestCase):
    def test_one_hundred_concurrent_http_booking_attempts(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL for meaningful row-lock contention")

        session = Session.objects.create(
            name="HTTP load test session", status="active", semester_type="odd"
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.localdate() + timedelta(days=5),
            booking_deadline=timezone.now() + timedelta(days=2),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        # Fifty users compete for ten spots, thirty for eight, twenty for six.
        slot_specs = ((50, 10), (30, 8), (20, 6))
        slots = [
            Slot.objects.create(
                exam_date=exam_date,
                shift=shift,
                total_positions=capacity,
            )
            for (_, capacity), shift in zip(
                slot_specs, ("Morning", "Evening", "Other")
            )
        ]

        tasks = []
        for i in range(100):
            user = User.objects.create_user(username=f"http-load-user-{i}")
            Teacher.objects.create(
                user=user,
                faculty_id=f"HTTP-LOAD-{i:03d}",
                full_name=f"Load Test Teacher {i}",
                role="permanent",
                final_duties=1,
            )
            group_index = 0 if i < 50 else (1 if i < 80 else 2)
            tasks.append((user, slots[group_index].pk))

        def send_request(task):
            user, slot_pk = task
            close_old_connections()
            client = Client()
            start = perf_counter()
            try:
                client.force_login(user)
                response = client.post(reverse("book_slot", args=[slot_pk]))
                return response.status_code, perf_counter() - start
            finally:
                close_old_connections()

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(send_request, task) for task in tasks]
            measurements = [future.result(timeout=90) for future in as_completed(futures)]
        elapsed = perf_counter() - started

        self.assertEqual(len(measurements), 100)
        self.assertEqual(
            [status for status, _duration in measurements if status != 302],
            [],
            "Unexpected HTTP errors during booking load",
        )
        self.assertEqual(Booking.objects.filter(slot__exam_date__session=session).count(), 24)

        for slot, (_attempts, capacity) in zip(slots, slot_specs):
            slot.refresh_from_db()
            self.assertEqual(slot.filled_positions, capacity)
            self.assertEqual(Booking.objects.filter(slot=slot).count(), capacity)

        teachers = list(Teacher.objects.filter(faculty_id__startswith="HTTP-LOAD-"))
        booking_counts = dict(
            (teacher.pk, Booking.objects.filter(
                teacher=teacher,
                slot__exam_date__session=session,
            ).count())
            for teacher in teachers
        )
        self.assertEqual(len(teachers), 100)
        for teacher in teachers:
            self.assertLessEqual(booking_counts[teacher.pk], 1)
            self.assertEqual(teacher.final_duties + booking_counts[teacher.pk], 1)

        durations = sorted(duration for _, duration in measurements)
        p95 = durations[int(len(durations) * 0.95) - 1]
        logger.warning(
            "100-user HTTP load: attempts=100 bookings=24 workers=12 "
            "elapsed=%.2fs median=%.3fs p95=%.3fs max=%.3fs",
            elapsed, median(durations), p95, durations[-1],
        )
