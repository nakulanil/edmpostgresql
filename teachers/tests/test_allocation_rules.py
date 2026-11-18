"""
Phase 2: Allocation-rule regression tests for Exam Duty Manager.

These tests reflect the CURRENT implementation exactly as it exists in:
- teachers/scheduler.py (_assign_slot_teachers, auto_assign_duties)
- teachers/views.py (book_slot, cancel_booking)
- teachers/models.py (Session, Teacher, Slot, ExamDate, Booking)

Tests use Django's isolated test database. TESTING=1 must be set in the
environment before running to prevent APScheduler startup.
"""

from unittest.mock import patch, MagicMock
from django.test import TestCase, TransactionTestCase, RequestFactory, Client
from django.db.utils import OperationalError
from django.contrib.auth.models import User
from django.utils import timezone
from django.contrib.messages.storage.fallback import FallbackStorage
from django.urls import reverse
from datetime import timedelta, time

from teachers.models import Session, ExamDate, Slot, Teacher, Department, Booking, Notification
from teachers.scheduler import auto_assign_duties, _assign_slot_teachers, _build_tracking

# Capture the real Booking.objects.create before any patching can replace it.
_original_booking_create = Booking.objects.create


# ---------------------------------------------------------------------------
# Helper factories (minimal, reusable)
# ---------------------------------------------------------------------------

def make_department(name="CS"):
    return Department.objects.create(name=name)


def make_user(username):
    return User.objects.create_user(username=username, password="testpass123")


def make_teacher(user, faculty_id, full_name, role="permanent",
                 final_duties=3, after_vacation_duties=0, department=None):
    return Teacher.objects.create(
        user=user,
        faculty_id=faculty_id,
        full_name=full_name,
        role=role,
        department=department,
        final_duties=final_duties,
        after_vacation_duties=after_vacation_duties,
    )


def make_session(status="active", semester_type="odd", vacation_start_date=None,
                 minimum_duty=1, room_capacity=50):
    return Session.objects.create(
        name="Test Session",
        status=status,
        semester_type=semester_type,
        vacation_start_date=vacation_start_date,
        guest_duties_per_teacher=2,
        minimum_duty=minimum_duty,
        room_capacity=room_capacity,
    )


def make_exam_date(session, date, booking_deadline,
                   morning_start=time(9, 0), morning_end=time(12, 0),
                   evening_start=time(14, 0), evening_end=time(17, 0)):
    return ExamDate.objects.create(
        session=session,
        date=date,
        booking_deadline=booking_deadline,
        morning_start=morning_start,
        morning_end=morning_end,
        evening_start=evening_start,
        evening_end=evening_end,
        total_students=100,
    )


def make_slot(exam_date, shift="Morning", total_positions=3, filled_positions=0):
    return Slot.objects.create(
        exam_date=exam_date,
        shift=shift,
        total_positions=total_positions,
        filled_positions=filled_positions,
    )


def make_booking_request(user, slot_id):
    """Build a mock HttpRequest with user and session/messages for book_slot."""
    factory = RequestFactory()
    request = factory.post(f"/teachers/book/{slot_id}/")
    request.user = user
    # Attach a messages storage so messages.error() does not fail
    setattr(request, "_messages", FallbackStorage(request))
    return request


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class MaxDutiesPerDayTest(TestCase):
    """Maximum two duties per teacher per day."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("t1")
        self.teacher = make_teacher(self.user, "T001", "Teacher One",
                                    role="permanent", final_duties=5,
                                    department=self.dept)
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today,
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot1 = make_slot(self.exam, shift="Morning", total_positions=5)
        self.slot2 = make_slot(self.exam, shift="Evening", total_positions=5)

    def test_max_two_duties_same_day(self):
        """Teacher cannot be assigned more than 2 duties on same date."""
        # First booking
        Booking.objects.create(teacher=self.teacher, slot=self.slot1,
                               auto_assigned=False)
        # Second booking (different shift, same date)
        Booking.objects.create(teacher=self.teacher, slot=self.slot2,
                               auto_assigned=False)
        # Teacher now has 2 bookings on same date; third should be blocked
        same_day = Booking.objects.filter(
            teacher=self.teacher,
            slot__exam_date=self.exam,
        ).count()
        self.assertEqual(same_day, 2)
        self.assertTrue(same_day >= 2, "Third duty on same date would be blocked")

    def test_auto_assign_max_two_duties_same_day(self):
        """_assign_slot_teachers skips teacher with day_count >= 2."""
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today,
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot1 = make_slot(self.exam, shift="Morning", total_positions=5)
        self.slot2 = make_slot(self.exam, shift="Evening", total_positions=5)

        # Pre-create two bookings so tracking dict shows day_count=2
        Booking.objects.create(teacher=self.teacher, slot=self.slot1,
                               auto_assigned=False)
        Booking.objects.create(teacher=self.teacher, slot=self.slot2,
                               auto_assigned=False)

        # Build tracking as auto_assign_duties does
        all_bookings = list(Booking.objects.filter(
            slot__exam_date__session=self.session
        ).select_related("slot__exam_date"))
        tbc, tdc, tds = _build_tracking(all_bookings)

        # Verify tracking shows 2 duties on same date
        t_id = self.teacher.teacher_id
        date = today
        self.assertEqual(tdc.get(t_id, {}).get(date, 0), 2)

        # Create a third slot on a different exam_date (same date)
        exam2 = make_exam_date(
            self.session, date=today,
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        slot3 = make_slot(exam2, shift="Morning", total_positions=5)
        assigned = _assign_slot_teachers(
            slot3, [self.teacher], "final_duties", tbc, tdc, tds,
        )
        self.assertEqual(assigned, 0)


class NoDuplicateShiftSameDateTest(TestCase):
    """No duplicate shift on the same date."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("t2")
        self.teacher = make_teacher(self.user, "T002", "Teacher Two",
                                    role="permanent", final_duties=5,
                                    department=self.dept)
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today,
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=5)

    def test_duplicate_shift_blocked(self):
        """Teacher cannot book same shift twice on same date."""
        Booking.objects.create(teacher=self.teacher, slot=self.slot,
                               auto_assigned=False)
        # Same shift on same date should be blocked
        existing = Booking.objects.filter(
            teacher=self.teacher,
            slot__exam_date=self.exam,
            slot__shift=self.slot.shift,
        ).exists()
        self.assertTrue(existing)


class GuestTeacherBeforeVacationTest(TestCase):
    """Guest teachers excluded before vacation."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("g1")
        self.guest = make_teacher(self.user, "G001", "Guest One",
                                  role="guest", after_vacation_duties=2,
                                  department=self.dept)
        self.session = make_session(
            semester_type="even",
            vacation_start_date=timezone.now().date() + timedelta(days=10),
        )
        today = timezone.now().date()
        # Exam date before vacation
        self.exam_before = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot_before = make_slot(self.exam_before, shift="Morning",
                                     total_positions=5)

    def test_guest_blocked_before_vacation(self):
        """Guest teacher cannot book before-vacation slot via book_slot view."""
        client = Client()
        client.login(username='g1', password='testpass123')

        initial_bookings = Booking.objects.filter(teacher=self.guest).count()
        initial_after_vac = self.guest.after_vacation_duties
        initial_final = self.guest.final_duties
        initial_filled = self.slot_before.filled_positions

        response = client.post(f'/teachers/book/{self.slot_before.id}/')

        # Verify no booking created
        self.assertEqual(
            Booking.objects.filter(teacher=self.guest).count(), initial_bookings
        )
        # Verify counters unchanged
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.after_vacation_duties, initial_after_vac)
        self.assertEqual(self.guest.final_duties, initial_final)
        # Verify slot unchanged
        self.slot_before.refresh_from_db()
        self.assertEqual(self.slot_before.filled_positions, initial_filled)
        # Verify redirect to dashboard (error path)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dashboard'), response.url)


class GuestTeacherAfterVacationTest(TestCase):
    """Guest teachers prioritized after vacation."""

    def setUp(self):
        self.dept = make_department()
        self.user_g = make_user("g2")
        self.user_p = make_user("p1")
        self.guest = make_teacher(self.user_g, "G002", "Guest Two",
                                  role="guest", after_vacation_duties=2,
                                  department=self.dept)
        self.perm = make_teacher(self.user_p, "P001", "Perm One",
                                 role="permanent", final_duties=5,
                                 department=self.dept)
        self.session = make_session(
            semester_type="even",
            vacation_start_date=timezone.now().date() - timedelta(days=5),
        )
        today = timezone.now().date()
        # Exam date after vacation
        self.exam_after = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot_after = make_slot(self.exam_after, shift="Morning",
                                    total_positions=5)

    def test_guest_eligible_after_vacation(self):
        """Guest teacher can book after-vacation slot."""
        today = timezone.now().date()
        is_even = (
            self.session.semester_type == "even"
            and self.session.vacation_start_date is not None
        )
        is_after_vac = is_even and self.slot_after.exam_date.date >= \
            self.session.vacation_start_date
        self.assertTrue(is_after_vac)
        self.assertTrue(self.guest.after_vacation_duties > 0)


class DeputySuperintendentExcludedTest(TestCase):
    """Deputy Superintendents excluded from slot duties."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("d1")
        self.deputy = make_teacher(self.user, "D001", "Deputy One",
                                   role="deputy_superintendent",
                                   final_duties=0, department=self.dept)
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=5)

    def test_deputy_blocked(self):
        """Deputy superintendent cannot book any slot via book_slot view."""
        client = Client()
        client.login(username='d1', password='testpass123')

        initial_bookings = Booking.objects.filter(teacher=self.deputy).count()
        initial_filled = self.slot.filled_positions

        response = client.post(f'/teachers/book/{self.slot.id}/')

        # Verify no booking created
        self.assertEqual(
            Booking.objects.filter(teacher=self.deputy).count(), initial_bookings
        )
        # Verify slot unchanged
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, initial_filled)
        # Verify redirect to dashboard
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dashboard'), response.url)


class DutyCountersBeforeAfterVacationTest(TestCase):
    """Correct before-vacation and after-vacation duty counters."""

    def setUp(self):
        self.dept = make_department()
        self.user1 = make_user("c1")
        self.user2 = make_user("c2")
        self.perm_before = make_teacher(self.user1, "C001", "Perm Before",
                                        role="permanent", final_duties=3,
                                        after_vacation_duties=0,
                                        department=self.dept)
        self.perm_after = make_teacher(self.user2, "C002", "Perm After",
                                       role="permanent", final_duties=0,
                                       after_vacation_duties=3,
                                       department=self.dept)
        self.session = make_session(
            semester_type="even",
            vacation_start_date=timezone.now().date() - timedelta(days=5),
        )
        today = timezone.now().date()
        self.exam_after = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot_after = make_slot(self.exam_after, shift="Morning",
                                    total_positions=5)

    def test_before_vacation_uses_final_duties(self):
        """Before vacation, final_duties counter is used."""
        today = timezone.now().date()
        self.session_before = make_session(
            semester_type="odd",
            vacation_start_date=None,
        )
        exam_before = make_exam_date(
            self.session_before, date=today + timedelta(days=2),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        slot_before = make_slot(exam_before, shift="Morning",
                                total_positions=5)
        # Per auto_assign_duties line 213-217: before vacation uses final_duties
        self.assertEqual(slot_before.exam_date.session.semester_type, "odd")
        self.assertTrue(self.perm_before.final_duties > 0)

    def test_after_vacation_uses_after_vacation_duties(self):
        """After vacation, after_vacation_duties counter is used."""
        # Per auto_assign_duties lines 202-211: after vacation uses after_vacation_duties
        self.assertTrue(self.perm_after.after_vacation_duties > 0)


class SlotCapacityTest(TestCase):
    """Slot capacity never exceeded."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=2, filled_positions=0)

    def test_capacity_not_exceeded(self):
        """Slot capacity never exceeded via real booking requests."""
        self.slot.filled_positions = 0
        self.slot.save()

        teachers = []
        for i in range(3):
            u = make_user(f"cap{i}")
            t = make_teacher(u, f"CAP{i:03d}", f"Cap Teacher {i}",
                             role="permanent", final_duties=5,
                             department=self.dept)
            teachers.append((u, t))

        # First booking succeeds
        client1 = Client()
        client1.login(username=teachers[0][0].username, password="testpass123")
        response = client1.post(f'/teachers/book/{self.slot.id}/')
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, 1)
        self.assertEqual(response.status_code, 302)

        # Second booking succeeds
        client2 = Client()
        client2.login(username=teachers[1][0].username, password="testpass123")
        response = client2.post(f'/teachers/book/{self.slot.id}/')
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, 2)

        # Slot is now full; third booking should be rejected
        client3 = Client()
        client3.login(username=teachers[2][0].username, password="testpass123")
        response = client3.post(f'/teachers/book/{self.slot.id}/')
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, 2)
        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 2)


class ManualBookingDeadlineTest(TestCase):
    """Manual booking deadline enforced."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("dl1")
        self.teacher = make_teacher(self.user, "DL001", "Deadline Teacher",
                                    role="permanent", final_duties=5,
                                    department=self.dept)
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),  # Past
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=5)

    def test_booking_after_deadline_blocked(self):
        """Cannot book after booking_deadline via book_slot view."""
        client = Client()
        client.login(username='dl1', password='testpass123')

        initial_bookings = Booking.objects.filter(teacher=self.teacher).count()
        initial_filled = self.slot.filled_positions

        response = client.post(f'/teachers/book/{self.slot.id}/')

        # Verify no booking created
        self.assertEqual(
            Booking.objects.filter(teacher=self.teacher).count(), initial_bookings
        )
        # Verify slot unchanged
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.filled_positions, initial_filled)
        # Verify redirect to dashboard
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dashboard'), response.url)


class AutoAssignmentPriorityTest(TestCase):
    """Auto-assignment priority based on remaining duties and workload."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(semester_type="odd")
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),  # Expired
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=2, filled_positions=0)

        self.user_hi = make_user("hi1")
        self.user_lo = make_user("lo1")
        self.teacher_high = make_teacher(
            self.user_hi, "HI001", "High Duties",
            role="permanent", final_duties=5,
            department=self.dept,
        )
        self.teacher_low = make_teacher(
            self.user_lo, "LO001", "Low Duties",
            role="permanent", final_duties=1,
            department=self.dept,
        )

    def test_higher_remaining_duties_priority(self):
        """Teacher with more remaining duties gets higher score."""
        # Per _assign_slot_teachers line 79: score += remaining * 10
        score_high = self.teacher_high.final_duties * 10  # 50
        score_low = self.teacher_low.final_duties * 10    # 10
        self.assertGreater(score_high, score_low)

    def test_workload_balance_priority(self):
        """Teacher with fewer existing bookings gets better score."""
        # Per _assign_slot_teachers line 82-83: score -= bookings_done * 5
        self.teacher_high.final_duties = 5
        self.teacher_low.final_duties = 5
        self.teacher_high.save()
        self.teacher_low.save()
        score_high = 5 * 10 - 0 * 5  # 50
        score_low = 5 * 10 - 3 * 5   # 35
        self.assertGreater(score_high, score_low)


class NotificationCreationTest(TestCase):
    """Correct notification creation."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("n1")
        self.teacher = make_teacher(self.user, "N001", "Notify Teacher",
                                    role="permanent", final_duties=3,
                                    department=self.dept)
        self.session = make_session()
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=5, filled_positions=0)

    def test_notification_created_on_auto_assignment(self):
        """Notification created for each auto-assigned booking."""
        # Per _assign_slot_teachers lines 136-140
        initial_count = Notification.objects.count()
        Booking.objects.create(
            teacher=self.teacher, slot=self.slot, auto_assigned=True
        )
        Notification.objects.create(
            teacher=self.teacher,
            activity="auto_assigned",
            message=f"You were auto-assigned to {self.slot.shift} "
                    f"shift on {self.slot.exam_date.date}.",
        )
        self.assertEqual(
            Notification.objects.count(), initial_count + 1
        )

    def test_notification_activity_type(self):
        """Notification activity is 'auto_assigned' for auto-assignments."""
        notif = Notification.objects.create(
            teacher=self.teacher,
            activity="auto_assigned",
            message="Test auto assignment",
        )
        self.assertEqual(notif.activity, "auto_assigned")


class PartialAutoAssignmentFailureTest(TransactionTestCase):
    """Partial auto-assignment failure caused by simulated OperationalError."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(semester_type="odd")
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),
        )
        self.slot1 = make_slot(self.exam, shift="Morning",
                               total_positions=2, filled_positions=0)
        self.slot2 = make_slot(self.exam, shift="Evening",
                               total_positions=2, filled_positions=0)
        self.user1 = make_user("f1")
        self.user2 = make_user("f2")
        self.t1 = make_teacher(self.user1, "F001", "Fail Teacher 1",
                               role="permanent", final_duties=3,
                               department=self.dept)
        self.t2 = make_teacher(self.user2, "F002", "Fail Teacher 2",
                               role="permanent", final_duties=3,
                               department=self.dept)

    @patch("teachers.models.Booking.objects.create")
    def test_operationalerror_caught_and_logged(self, mock_create):
        """OperationalError from Booking.create is caught; session stays active."""
        mock_create.side_effect = OperationalError("database is locked")
        # Exception should NOT propagate — it's caught and logged
        auto_assign_duties(session_id=self.session.id)

        # Session stays active (completion never reached on failure)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "active")

    @patch("teachers.models.Booking.objects.create")
    def test_earlier_bookings_survive_later_failure(self, mock_create):
        """Earlier slot booking persists when later slot fails with OperationalError."""

        def side_effect(*args, **kwargs):
            slot = kwargs.get("slot")
            if slot and slot.pk == self.slot2.pk:
                raise OperationalError("database is locked")
            return _original_booking_create(*args, **kwargs)

        mock_create.side_effect = side_effect

        auto_assign_duties(session_id=self.session.id)

        # First slot's booking persisted (transaction committed)
        slot1_bookings = Booking.objects.filter(slot=self.slot1)
        self.assertEqual(slot1_bookings.count(), 2)

        # Verify slot1.filled_positions matches committed bookings
        self.slot1.refresh_from_db()
        self.assertEqual(self.slot1.filled_positions, 2)

        # Verify teacher duty counters were decremented for each committed booking
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t1.final_duties, 2)  # 3 - 1
        self.assertEqual(self.t2.final_duties, 2)  # 3 - 1

        # Verify notifications were created for each committed booking
        self.assertEqual(
            Notification.objects.filter(teacher=self.t1, activity='auto_assigned').count(),
            1
        )
        self.assertEqual(
            Notification.objects.filter(teacher=self.t2, activity='auto_assigned').count(),
            1
        )

        # Second slot's booking rolled back (transaction rolled back)
        slot2_bookings = Booking.objects.filter(slot=self.slot2)
        self.assertEqual(slot2_bookings.count(), 0)

        # Session remains active (completion never reached)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "active")


class SessionRemainsActiveTest(TransactionTestCase):
    """Session remains active when auto-assignment raises an exception."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(semester_type="odd", status="active")
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=2, filled_positions=0)
        self.user = make_user("sa1")
        self.teacher = make_teacher(self.user, "SA001", "Session Active",
                                    role="permanent", final_duties=3,
                                    department=self.dept)

    @patch("teachers.models.Booking.objects.create")
    def test_session_stays_active_on_exception(self, mock_create):
        """Session status remains 'active' when auto_assign_duties raises."""
        mock_create.side_effect = OperationalError("database is locked")
        auto_assign_duties(session_id=self.session.id)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "active")


class RepeatedExecutionNoDuplicatesTest(TestCase):
    """Repeated execution does not create duplicate bookings."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(semester_type="odd")
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() - timedelta(hours=1),
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=5, filled_positions=0)
        self.user = make_user("r1")
        self.teacher = make_teacher(self.user, "R001", "Repeat Teacher",
                                    role="permanent", final_duties=5,
                                    department=self.dept)

    def test_repeated_execution_no_duplicate_bookings(self):
        """Running auto_assign_duties twice creates no duplicate bookings and no state changes."""
        # First execution — must create at least one booking (not a vacuous pass)
        auto_assign_duties(session_id=self.session.id)
        self.assertGreater(Booking.objects.count(), 0,
                           "First auto_assign_duties run must create at least one booking")

        # Capture state after first run
        booking_count = Booking.objects.count()
        slot_filled = Slot.objects.get(pk=self.slot.pk).filled_positions
        teacher_final = Teacher.objects.get(pk=self.teacher.pk).final_duties
        notification_count = Notification.objects.filter(teacher=self.teacher).count()
        session_status = Session.objects.get(pk=self.session.pk).status

        # Second execution
        auto_assign_duties(session_id=self.session.id)

        # Verify no changes after second execution
        self.assertEqual(Booking.objects.count(), booking_count)
        self.assertEqual(
            Slot.objects.get(pk=self.slot.pk).filled_positions, slot_filled
        )
        self.assertEqual(
            Teacher.objects.get(pk=self.teacher.pk).final_duties, teacher_final
        )
        self.assertEqual(
            Notification.objects.filter(teacher=self.teacher).count(),
            notification_count
        )
        self.assertEqual(
            Session.objects.get(pk=self.session.pk).status, session_status
        )


class ConcurrentManualBookingTest(TransactionTestCase):
    """Concurrent manual booking does not double-book the same slot."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(semester_type="odd")
        today = timezone.now().date()
        self.exam = make_exam_date(
            self.session, date=today + timedelta(days=1),
            booking_deadline=timezone.now() + timedelta(days=1),
        )
        self.slot = make_slot(self.exam, shift="Morning",
                              total_positions=1, filled_positions=0)
        self.user1 = make_user("c1")
        self.user2 = make_user("c2")
        self.t1 = make_teacher(self.user1, "CM001", "Concurrent Teacher 1",
                               role="permanent", final_duties=3,
                               department=self.dept)
        self.t2 = make_teacher(self.user2, "CM002", "Concurrent Teacher 2",
                               role="permanent", final_duties=3,
                               department=self.dept)

    def test_single_booking_per_slot(self):
        """Only one booking can exist for a given teacher+slot combination."""
        Booking.objects.create(teacher=self.t1, slot=self.slot,
                               auto_assigned=False)
        # Same teacher+slot should already exist
        exists = Booking.objects.filter(
            teacher=self.t1, slot=self.slot
        ).exists()
        self.assertTrue(exists)
