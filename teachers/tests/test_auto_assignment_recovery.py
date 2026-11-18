"""

Phase 3 Batch 2A & 2C: Regression tests for safe auto-assignment recovery.



These tests define expected behavior for safe auto-assignment recovery,

including session-specific execution, idempotency, retry safety, and

correct completion semantics.

"""



from unittest.mock import patch, MagicMock

from django.test import TestCase, TransactionTestCase

from django.db.utils import OperationalError

from django.contrib.auth.models import User

from django.utils import timezone

from datetime import timedelta, time



from django_apscheduler.models import DjangoJob

from teachers.models import Session, ExamDate, Slot, Teacher, Department, Booking, Notification

from teachers.scheduler import auto_assign_duties, scheduler, schedule_auto_assignment, start



# Capture the REAL Booking.objects.create before any test patching can

# replace it. This avoids infinite recursion when side_effect calls

# the original create inside a patched context.

_real_booking_create = Booking.objects.create





def make_department(name="CS"):

    return Department.objects.create(name=name)





def make_user(username):

    return User.objects.create_user(username=username, password="testpass123")





def make_teacher(user, faculty_id, full_name, role="permanent",

                 final_duties=3, after_vacation_duties=0, department=None):

    return Teacher.objects.create(

        user=user, faculty_id=faculty_id, full_name=full_name,

        role=role, department=department,

        final_duties=final_duties,

        after_vacation_duties=after_vacation_duties,

    )





def make_session(status="active", semester_type="odd", vacation_start_date=None):

    return Session.objects.create(

        name="Test Session", status=status, semester_type=semester_type,

        vacation_start_date=vacation_start_date,

    )





def make_exam_date(session, days_offset, deadline_offset_hours):

    return ExamDate.objects.create(

        session=session,

        date=timezone.now().date() + timedelta(days=days_offset),

        booking_deadline=timezone.now() + timedelta(hours=deadline_offset_hours),

        morning_start=time(9, 0), morning_end=time(12, 0),

        evening_start=time(14, 0), evening_end=time(17, 0),

        total_students=100,

    )





def make_slot(exam_date, shift="Morning", total_positions=2, filled_positions=0):

    return Slot.objects.create(

        exam_date=exam_date, shift=shift,

        total_positions=total_positions, filled_positions=filled_positions,

    )





def _clear_scheduler_jobs():

    """Clear all scheduler jobs from DjangoJobStore."""

    DjangoJob.objects.all().delete()





# ---------------------------------------------------------------------------

# 1. Session-specific execution

# ---------------------------------------------------------------------------



class SessionSpecificExecutionTest(TestCase):

    """Auto-assignment must process only the intended session."""



    def setUp(self):

        self.dept = make_department()

        self.session1 = make_session(status="active", semester_type="odd")

        self.user1 = make_user("s1t1")

        self.t1 = make_teacher(self.user1, "S1T001", "Session1 Teacher 1",

                               role="permanent", final_duties=3,

                               department=self.dept)

        self.exam1 = make_exam_date(self.session1, days_offset=1,

                                    deadline_offset_hours=-2)

        self.slot1 = make_slot(self.exam1, shift="Morning",

                               total_positions=2, filled_positions=0)



        self.session2 = make_session(status="active", semester_type="odd")

        self.user2 = make_user("s2t1")

        self.t2 = make_teacher(self.user2, "S2T001", "Session2 Teacher 1",

                               role="permanent", final_duties=3,

                               department=self.dept)

        self.exam2 = make_exam_date(self.session2, days_offset=1,

                                    deadline_offset_hours=-2)

        self.slot2 = make_slot(self.exam2, shift="Morning",

                               total_positions=2, filled_positions=0)



    def test_assigns_only_first_active_session(self):

        """auto_assign_duties(session_id) processes only the intended session."""

        auto_assign_duties(session_id=self.session1.id)



        s1_bookings = Booking.objects.filter(slot=self.slot1)

        self.assertGreater(s1_bookings.count(), 0,

                           "First active session's slots should get bookings")



        s2_bookings = Booking.objects.filter(slot=self.slot2)

        self.assertEqual(s2_bookings.count(), 0,

                         "Second active session should not be processed")



        self.session2.refresh_from_db()

        self.assertEqual(self.session2.status, "active")





# ---------------------------------------------------------------------------

# 2. Partial failure and retry

# ---------------------------------------------------------------------------



class PartialFailureRetryTest(TransactionTestCase):

    """Partial failure must be retryable without losing committed work."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



        self.exam1 = make_exam_date(self.session, days_offset=1,

                                    deadline_offset_hours=-2)

        self.exam2 = make_exam_date(self.session, days_offset=1,

                                    deadline_offset_hours=-2)



        self.slot1 = make_slot(self.exam1, shift="Morning",

                               total_positions=2, filled_positions=0)

        self.slot2 = make_slot(self.exam2, shift="Evening",

                               total_positions=1, filled_positions=0)



        self.user1 = make_user("pf1")

        self.t1 = make_teacher(self.user1, "PF001", "Partial Teacher 1",

                               role="permanent", final_duties=3,

                               department=self.dept)

        self.user2 = make_user("pf2")

        self.t2 = make_teacher(self.user2, "PF002", "Partial Teacher 2",

                               role="permanent", final_duties=3,

                               department=self.dept)



    @patch("teachers.models.Booking.objects.create")

    def test_first_slot_commits_later_slot_fails(self, mock_create):

        """First slot booking commits; later slot failure propagates."""



        def side_effect(*args, **kwargs):

            slot = kwargs.get("slot")

            if slot and slot.pk == self.slot2.pk:

                raise OperationalError("database is locked")

            return _real_booking_create(*args, **kwargs)



        mock_create.side_effect = side_effect



        auto_assign_duties(session_id=self.session.id)



        slot1_bookings = Booking.objects.filter(slot=self.slot1)

        self.assertEqual(slot1_bookings.count(), 2,

                         "First slot should have both positions filled")



        slot2_bookings = Booking.objects.filter(slot=self.slot2)

        self.assertEqual(slot2_bookings.count(), 0,

                         "Second slot should have no bookings after failure")



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active")



    def test_retry_processes_remaining_slots_no_duplicates(self):

        """On retry, remaining slots processed; no duplicate bookings."""

        failed_slot_pk = self.slot2.pk



        def side_effect(*args, **kwargs):

            slot = kwargs.get("slot")

            if slot and slot.pk == failed_slot_pk:

                raise OperationalError("database is locked")

            return _real_booking_create(*args, **kwargs)



        with patch("teachers.models.Booking.objects.create") as mock_create:

            mock_create.side_effect = side_effect

            auto_assign_duties(session_id=self.session.id)



        slot1_bookings_before = Booking.objects.filter(slot=self.slot1).count()

        self.assertGreater(slot1_bookings_before, 0,

                           "First slot should have bookings after first run")



        auto_assign_duties(session_id=self.session.id)



        slot1_bookings_after = Booking.objects.filter(slot=self.slot1).count()

        self.assertEqual(slot1_bookings_after, slot1_bookings_before,

                         "Slot 1 should not get duplicate bookings on retry")



        slot2_bookings = Booking.objects.filter(slot=self.slot2)

        self.assertGreater(slot2_bookings.count(), 0,

                           "Slot 2 should be processed on retry")



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "completed")





# ---------------------------------------------------------------------------

# 3. Duty-counter consistency

# ---------------------------------------------------------------------------



class DutyCounterConsistencyTest(TestCase):

    """Teacher duty counters must remain consistent."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.exam = make_exam_date(self.session, days_offset=1,

                                   deadline_offset_hours=-2)

        self.slot = make_slot(self.exam, shift="Morning",

                              total_positions=2, filled_positions=0)



        self.user1 = make_user("dc1")

        self.t1 = make_teacher(self.user1, "DC001", "Counter Teacher 1",

                               role="permanent", final_duties=3,

                               department=self.dept)

        self.user2 = make_user("dc2")

        self.t2 = make_teacher(self.user2, "DC002", "Counter Teacher 2",

                               role="permanent", final_duties=3,

                               department=self.dept)



    def test_counter_decrements_once_per_booking(self):

        """Each committed booking decrements teacher counter exactly once."""

        auto_assign_duties(session_id=self.session.id)



        self.t1.refresh_from_db()

        self.t2.refresh_from_db()



        self.assertEqual(self.t1.final_duties, 2,

                         "Teacher 1 counter should decrement by 1")

        self.assertEqual(self.t2.final_duties, 2,

                         "Teacher 2 counter should decrement by 1")



    def test_no_counter_becomes_negative(self):

        """Teacher duty counter never goes below zero."""

        self.t1.final_duties = 1

        self.t1.save()

        self.slot.total_positions = 3

        self.slot.save()



        auto_assign_duties(session_id=self.session.id)



        self.t1.refresh_from_db()

        self.assertGreaterEqual(self.t1.final_duties, 0,

                                "Counter should not go negative")



    def test_failed_slot_does_not_consume_duties(self):

        """Failed slot transaction does not consume teacher duties."""



        def side_effect(*args, **kwargs):

            raise OperationalError("database is locked")



        with patch("teachers.models.Booking.objects.create") as mock_create:

            mock_create.side_effect = side_effect

            auto_assign_duties(session_id=self.session.id)



        self.t1.refresh_from_db()

        self.t2.refresh_from_db()



        self.assertEqual(self.t1.final_duties, 3,

                         "Teacher 1 counter unchanged after total failure")

        self.assertEqual(self.t2.final_duties, 3,

                         "Teacher 2 counter unchanged after total failure")





# ---------------------------------------------------------------------------

# 4. Mixed deadlines

# ---------------------------------------------------------------------------



class MixedDeadlinesTest(TestCase):

    """Expired and future ExamDates must be handled correctly."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



        self.user = make_user("md1")

        self.teacher = make_teacher(self.user, "MD001", "Mixed Deadline Teacher",

                                    role="permanent", final_duties=5,

                                    department=self.dept)



        self.expired_exam = make_exam_date(self.session, days_offset=1,

                                           deadline_offset_hours=-2)

        self.expired_slot = make_slot(self.expired_exam, shift="Morning",

                                      total_positions=2, filled_positions=0)



        self.future_exam = make_exam_date(self.session, days_offset=5,

                                          deadline_offset_hours=24)

        self.future_slot = make_slot(self.future_exam, shift="Morning",

                                     total_positions=2, filled_positions=0)



    def test_expired_exam_dates_processed(self):

        """Expired ExamDates get bookings assigned."""

        auto_assign_duties(session_id=self.session.id)



        expired_bookings = Booking.objects.filter(slot=self.expired_slot)

        self.assertGreater(expired_bookings.count(), 0,

                           "Expired exam date slots should get bookings")



    def test_future_deadlines_untouched(self):

        """ExamDates with future deadlines remain untouched."""

        auto_assign_duties(session_id=self.session.id)



        future_bookings = Booking.objects.filter(slot=self.future_slot)

        self.assertEqual(future_bookings.count(), 0,

                         "Future exam date slots should not get bookings")



    def test_future_slot_counter_unchanged(self):

        """Teacher counter not decremented for future deadline slots."""

        auto_assign_duties(session_id=self.session.id)



        self.teacher.refresh_from_db()

        self.assertEqual(self.teacher.final_duties, 4,

                         "Counter should only reflect expired slot assignments")





# ---------------------------------------------------------------------------

# 5. Session completion

# ---------------------------------------------------------------------------



class SessionCompletionTest(TestCase):

    """Session completion must account for failures and future deadlines."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



        self.user = make_user("sc1")

        self.teacher = make_teacher(self.user, "SC001", "Completion Teacher",

                                    role="permanent", final_duties=3,

                                    department=self.dept)



    def test_not_completed_on_partial_failure(self):

        """Session stays active when assignment raises an exception."""

        self.exam = make_exam_date(self.session, days_offset=1,

                                   deadline_offset_hours=-2)

        self.slot = make_slot(self.exam, shift="Morning",

                              total_positions=1, filled_positions=0)



        with patch("teachers.models.Booking.objects.create") as mock_create:

            mock_create.side_effect = OperationalError("database is locked")

            auto_assign_duties(session_id=self.session.id)



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active",

                         "Session must remain active after partial failure")



    def test_not_completed_with_future_deadlines(self):

        """Session stays active while future deadlines remain."""

        self.future_exam = make_exam_date(self.session, days_offset=5,

                                          deadline_offset_hours=24)

        self.future_slot = make_slot(self.future_exam, shift="Morning",

                                     total_positions=2, filled_positions=0)



        auto_assign_duties(session_id=self.session.id)



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active",

                         "Session must stay active while future deadlines remain")



    def test_not_falsely_completed_on_teacher_shortage(self):

        """Session stays active when no eligible teachers remain for open slots."""

        self.exam = make_exam_date(self.session, days_offset=1,

                                   deadline_offset_hours=-2)

        self.slot = make_slot(self.exam, shift="Morning",

                              total_positions=2, filled_positions=0)



        self.teacher.final_duties = 0

        self.teacher.save()



        auto_assign_duties(session_id=self.session.id)



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active",

                         "Session must stay active when no eligible teachers remain")

        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 0,

                         "No bookings created when no eligible teachers")





# ---------------------------------------------------------------------------

# 6. Repeated execution

# ---------------------------------------------------------------------------



class RepeatedExecutionTest(TestCase):

    """Repeated execution must not change committed state."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.exam = make_exam_date(self.session, days_offset=1,

                                   deadline_offset_hours=-2)

        self.slot = make_slot(self.exam, shift="Morning",

                              total_positions=2, filled_positions=0)



        self.user1 = make_user("re1")

        self.t1 = make_teacher(self.user1, "RE001", "Repeat Teacher 1",

                               role="permanent", final_duties=3,

                               department=self.dept)

        self.user2 = make_user("re2")

        self.t2 = make_teacher(self.user2, "RE002", "Repeat Teacher 2",

                               role="permanent", final_duties=3,

                               department=self.dept)



    def test_no_changes_on_second_execution(self):

        """Running auto_assign_duties twice creates no state changes."""

        auto_assign_duties(session_id=self.session.id)



        booking_count_1 = Booking.objects.count()

        slot_filled_1 = Slot.objects.get(pk=self.slot.pk).filled_positions

        t1_duties_1 = Teacher.objects.get(pk=self.t1.pk).final_duties

        t2_duties_1 = Teacher.objects.get(pk=self.t2.pk).final_duties

        notif_count_1 = Notification.objects.count()

        session_status_1 = Session.objects.get(pk=self.session.pk).status



        auto_assign_duties(session_id=self.session.id)



        self.assertEqual(Booking.objects.count(), booking_count_1,

                         "No new bookings on second execution")

        self.assertEqual(Slot.objects.get(pk=self.slot.pk).filled_positions,

                         slot_filled_1,

                         "Slot counter unchanged on second execution")

        self.assertEqual(Teacher.objects.get(pk=self.t1.pk).final_duties,

                         t1_duties_1,

                         "Teacher 1 counter unchanged on second execution")

        self.assertEqual(Teacher.objects.get(pk=self.t2.pk).final_duties,

                         t2_duties_1,

                         "Teacher 2 counter unchanged on second execution")

        self.assertEqual(Notification.objects.count(), notif_count_1,

                         "No new notifications on second execution")

        self.assertEqual(Session.objects.get(pk=self.session.pk).status,

                         session_status_1,

                         "Session status unchanged on second execution")





# ---------------------------------------------------------------------------

# 7. Multiple deadlines per session

# ---------------------------------------------------------------------------



class MultipleDeadlinesTest(TestCase):

    """Session with three ExamDates, each with its own deadline."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user = make_user("md1")

        self.teacher = make_teacher(

            self.user, "MD201", "Multi Deadline Teacher",

            role="permanent", final_duties=10, department=self.dept,

        )

        self.exam1 = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-2,

        )

        self.exam2 = make_exam_date(

            self.session, days_offset=2, deadline_offset_hours=-4,

        )

        self.exam3 = make_exam_date(

            self.session, days_offset=3, deadline_offset_hours=24,

        )

        self.slot1 = make_slot(self.exam1, shift="Morning", total_positions=2)

        self.slot2 = make_slot(self.exam2, shift="Evening", total_positions=2)

        self.slot3 = make_slot(self.exam3, shift="Morning", total_positions=2)



    def test_three_distinct_deadlines(self):

        """Two expired ExamDates processed; one future ExamDate untouched."""

        auto_assign_duties(session_id=self.session.id)



        self.assertGreater(

            Booking.objects.filter(slot=self.slot1).count(), 0,

            "Slot 1 (expired) should have bookings",

        )

        self.assertGreater(

            Booking.objects.filter(slot=self.slot2).count(), 0,

            "Slot 2 (expired) should have bookings",

        )

        self.assertEqual(

            Booking.objects.filter(slot=self.slot3).count(), 0,

            "Slot 3 (future) should have no bookings",

        )

        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active")



    def test_deadline_order_irrelevant(self):

        """Processing order follows exam date, not deadline value."""

        auto_assign_duties(session_id=self.session.id)

        b1 = Booking.objects.filter(slot=self.slot1).count()

        b2 = Booking.objects.filter(slot=self.slot2).count()

        self.assertGreater(b1, 0, "First expired slot processed")

        self.assertGreater(b2, 0, "Second expired slot processed")





# ---------------------------------------------------------------------------

# 8. Unique scheduler jobs per ExamDate

# ---------------------------------------------------------------------------



class UniqueSchedulerJobTest(TestCase):

    """schedule_auto_assignment() generates unique job IDs per session+exam_date."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.exam1 = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.exam2 = make_exam_date(

            self.session, days_offset=2, deadline_offset_hours=-1,

        )



    def test_unique_job_ids(self):

        """Each ExamDate gets a distinct job ID."""

        _clear_scheduler_jobs()

        now = timezone.now()

        schedule_auto_assignment(now, self.session.id, self.exam1.id)

        schedule_auto_assignment(now, self.session.id, self.exam2.id)



        jobs = scheduler.get_jobs()

        job_ids = [j.id for j in jobs]

        expected_1 = f'auto_assign_duties_{self.session.id}_{self.exam1.id}'

        expected_2 = f'auto_assign_duties_{self.session.id}_{self.exam2.id}'

        self.assertIn(expected_1, job_ids, "Job ID for exam1 exists")

        self.assertIn(expected_2, job_ids, "Job ID for exam2 exists")

        self.assertNotEqual(expected_1, expected_2, "Job IDs are unique")



    def test_replace_existing_same_job(self):

        """Repeated scheduling of same ExamDate replaces, not duplicates."""

        _clear_scheduler_jobs()

        now = timezone.now()

        schedule_auto_assignment(now, self.session.id, self.exam1.id)

        schedule_auto_assignment(now, self.session.id, self.exam1.id)



        jobs = scheduler.get_jobs()

        matching = [j for j in jobs if j.id == f'auto_assign_duties_{self.session.id}_{self.exam1.id}']

        self.assertEqual(len(matching), 1, "Exactly one job for repeated scheduling")





# ---------------------------------------------------------------------------

# 9. Multiple sessions with pending scheduler jobs

# ---------------------------------------------------------------------------



class MultipleSessionsJobTest(TestCase):

    """Multiple active sessions each get independent scheduled jobs."""



    def setUp(self):

        self.dept = make_department()

        self.session1 = make_session(status="active", semester_type="odd")

        self.session2 = make_session(status="active", semester_type="odd")

        self.exam1 = make_exam_date(

            self.session1, days_offset=1, deadline_offset_hours=-1,

        )

        self.exam2 = make_exam_date(

            self.session2, days_offset=1, deadline_offset_hours=-1,

        )



    def test_independent_jobs_per_session(self):

        """Each session's ExamDate gets its own job ID."""

        _clear_scheduler_jobs()

        now = timezone.now()

        schedule_auto_assignment(now, self.session1.id, self.exam1.id)

        schedule_auto_assignment(now, self.session2.id, self.exam2.id)



        jobs = scheduler.get_jobs()

        job_ids = [j.id for j in jobs]

        id1 = f'auto_assign_duties_{self.session1.id}_{self.exam1.id}'

        id2 = f'auto_assign_duties_{self.session2.id}_{self.exam2.id}'

        self.assertIn(id1, job_ids, "Session 1 job exists")

        self.assertIn(id2, job_ids, "Session 2 job exists")

        self.assertNotEqual(id1, id2, "Job IDs differ across sessions")





# ---------------------------------------------------------------------------

# 10. Restart recovery with overdue deadlines

# ---------------------------------------------------------------------------



class RestartRecoveryTest(TestCase):

    """start() recovers overdue work and preserves future deadlines."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user = make_user("rr1")

        self.teacher = make_teacher(

            self.user, "RR001", "Recovery Teacher",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam_past = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-5,

        )

        self.exam_future = make_exam_date(

            self.session, days_offset=5, deadline_offset_hours=24,

        )

        self.slot_past = make_slot(self.exam_past, shift="Morning", total_positions=2)

        self.slot_future = make_slot(self.exam_future, shift="Morning", total_positions=2)



    def test_overdue_work_recovered_on_start(self):

        """start() discovers overdue sessions even without prior scheduler jobs."""

        _clear_scheduler_jobs()



        start()



        jobs = scheduler.get_jobs()

        overdue_jobs = [

            j for j in jobs if f'{self.session.id}_{self.exam_past.id}' in j.id

        ]

        self.assertGreater(

            len(overdue_jobs), 0, "Overdue job scheduled on start"

        )



        future_jobs = [

            j for j in jobs if f'{self.session.id}_{self.exam_future.id}' in j.id

        ]

        self.assertEqual(

            len(future_jobs), 0, "Future job not scheduled on start"

        )



        try:

            scheduler.shutdown(wait=True)

        except Exception:

            pass





# ---------------------------------------------------------------------------

# 11. Legacy scheduler job handling

# ---------------------------------------------------------------------------



class LegacyJobTest(TestCase):

    """Legacy auto_assign_duties job (no session/exam_date scoping) is cleaned up."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



    def test_legacy_job_removed(self):

        """Legacy job with id='auto_assign_duties' is deleted on start()."""

        DjangoJob.objects.create(

            id='auto_assign_duties',

            next_run_time=timezone.now(),

            job_state=b'',

        )



        _clear_scheduler_jobs()



        start()



        self.assertFalse(

            DjangoJob.objects.filter(id='auto_assign_duties').exists(),

            "Legacy job removed",

        )



        try:

            scheduler.shutdown(wait=True)

        except Exception:

            pass





# ---------------------------------------------------------------------------

# 12. Partially filled slots across retries

# ---------------------------------------------------------------------------



class PartialFillRetryTest(TestCase):

    """Partially filled slot is completed on retry without double-counting."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user1 = make_user("pf1")

        self.user2 = make_user("pf2")

        self.t1 = make_teacher(

            self.user1, "PF101", "Partial Teacher 1",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.t2 = make_teacher(

            self.user2, "PF102", "Partial Teacher 2",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.slot = make_slot(self.exam, shift="Morning", total_positions=3)



    def test_partial_fill_completed_on_retry(self):

        """Partially filled slot completed without double-counting."""

        self.slot.filled_positions = 1

        self.slot.save()



        auto_assign_duties(session_id=self.session.id)



        self.slot.refresh_from_db()

        self.assertEqual(self.slot.filled_positions, 3,

                         "Slot full after retry")



        total_bookings = Booking.objects.filter(slot=self.slot).count()

        self.assertEqual(total_bookings, 2,

                         "Exactly 2 auto-assigned bookings for remaining positions")



        self.t1.refresh_from_db()

        self.t2.refresh_from_db()

        total_decrease = (5 - self.t1.final_duties) + (5 - self.t2.final_duties)

        self.assertEqual(total_decrease, 2,

                         "Counter decrease equals auto-assigned booking count")





# ---------------------------------------------------------------------------

# 13. Counters matching actual booking changes

# ---------------------------------------------------------------------------



class CounterConsistencyDetailedTest(TestCase):

    """Duty counters match actual booking changes exactly."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user1 = make_user("cc1")

        self.user2 = make_user("cc2")

        self.t1 = make_teacher(

            self.user1, "CC101", "Counter Teacher 1",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.t2 = make_teacher(

            self.user2, "CC102", "Counter Teacher 2",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)



    def test_counters_match_bookings(self):

        """Each booking decrements the correct counter by exactly 1."""

        auto_assign_duties(session_id=self.session.id)



        self.t1.refresh_from_db()

        self.t2.refresh_from_db()



        bookings = Booking.objects.filter(slot=self.slot)

        self.assertEqual(bookings.count(), 2, "Two bookings for 2 positions")



        total_decrease = (5 - self.t1.final_duties) + (5 - self.t2.final_duties)

        self.assertEqual(total_decrease, 2,

                         "Counter decrease equals booking count")





# ---------------------------------------------------------------------------

# 14. Teacher shortage with partial assignment

# ---------------------------------------------------------------------------



class TeacherShortagePartialTest(TestCase):

    """Session stays active when some slots unfilled due to teacher shortage."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user1 = make_user("ts1")

        self.t1 = make_teacher(

            self.user1, "TS101", "Shortage Teacher",

            role="permanent", final_duties=1, department=self.dept,

        )

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.slot1 = make_slot(self.exam, shift="Morning", total_positions=1)

        self.slot2 = make_slot(self.exam, shift="Evening", total_positions=1)



    def test_partial_assignment_stays_active(self):

        """One slot filled, one unfilled; session stays active."""

        auto_assign_duties(session_id=self.session.id)



        total_bookings = (

            Booking.objects.filter(slot=self.slot1).count()

            + Booking.objects.filter(slot=self.slot2).count()

        )

        self.assertEqual(total_bookings, 1, "Only one slot filled")



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active")





# ---------------------------------------------------------------------------

# 15. Session with no ExamDate records

# ---------------------------------------------------------------------------



class NoExamDateTest(TestCase):

    """Session with no ExamDate records is not marked completed."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



    def test_empty_session_not_completed(self):

        """Session with no ExamDates stays active."""

        auto_assign_duties(session_id=self.session.id)



        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active",

                         "Empty session not auto-completed")





# ---------------------------------------------------------------------------

# 16. Concurrent execution protection

# ---------------------------------------------------------------------------



class ConcurrentExecutionTest(TransactionTestCase):

    """Concurrent auto_assign_duties calls don't create duplicate bookings."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user1 = make_user("ce1")

        self.user2 = make_user("ce2")

        self.t1 = make_teacher(

            self.user1, "CE101", "Concurrent Teacher 1",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.t2 = make_teacher(

            self.user2, "CE102", "Concurrent Teacher 2",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)



    def test_concurrent_execution_no_duplicates(self):

        """Repeated calls don't create duplicate bookings (SQLite serializes writes)."""

        auto_assign_duties(session_id=self.session.id)

        auto_assign_duties(session_id=self.session.id)



        total_bookings = Booking.objects.filter(slot=self.slot).count()

        self.assertEqual(total_bookings, 2,

                         "Exactly 2 bookings, no duplicates from repeated execution")



        self.slot.refresh_from_db()

        self.assertEqual(self.slot.filled_positions, 2,

                         "Slot filled positions matches bookings")



        self.t1.refresh_from_db()

        self.t2.refresh_from_db()

        self.assertGreaterEqual(self.t1.final_duties, 0)

        self.assertGreaterEqual(self.t2.final_duties, 0)





# ---------------------------------------------------------------------------

# 17. Session-wide completion with multiple expired ExamDates

# ---------------------------------------------------------------------------



class MultiExamCompletionTest(TestCase):

    """Session must not complete when one expired ExamDate still has open slots."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user1 = make_user("mc1")

        self.user2 = make_user("mc2")

        self.t1 = make_teacher(

            self.user1, "MC101", "Multi Exam Teacher 1",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.t2 = make_teacher(

            self.user2, "MC102", "Multi Exam Teacher 2",

            role="permanent", final_duties=1, department=self.dept,

        )

        # Two expired ExamDates

        self.exam1 = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-2,

        )

        self.exam2 = make_exam_date(

            self.session, days_offset=2, deadline_offset_hours=-2,

        )

        # E1 has 2 positions and 2 eligible teachers -> fully fillable

        self.slot1 = make_slot(self.exam1, shift="Morning", total_positions=2)

        # E2 has 2 positions but only 1 teacher with 1 duty remaining -> only 1 fillable

        self.slot2 = make_slot(self.exam2, shift="Morning", total_positions=2)



    def test_partial_session_not_completed(self):

        """Executing auto-assignment for E1 only leaves session active (E2 has open slots)."""

        auto_assign_duties(session_id=self.session.id, exam_date_id=self.exam1.id)



        # E1 slot should be fully filled

        self.assertEqual(

            Booking.objects.filter(slot=self.slot1).count(), 2,

            "E1 slot fully filled",

        )



        # Session must NOT be completed because E2 still has open slots

        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "active",

                         "Session stays active when not all expired ExamDates are fully assigned")



    def test_full_completion_when_all_done(self):

        """Session completes when all expired ExamDates are fully assigned."""

        # E1: 2 positions, 2 teachers with plenty of duties -> fully fillable

        # E2: 2 positions, but t2 only has 1 duty. t1 has 5 duties so can take both E2 slots

        # But max 2 duties per teacher per day. Both exams are on different days.

        # t1 can take 2 on E1 day and 2 on E2 day? No, max 2 per day.

        # Let's make it simpler: E1 has 1 position, E2 has 1 position, both fillable

        self.slot1.total_positions = 1

        self.slot1.save()

        self.slot2.total_positions = 1

        self.slot2.save()



        auto_assign_duties(session_id=self.session.id)



        # Both slots should be filled

        self.assertEqual(

            Booking.objects.filter(slot=self.slot1).count(), 1,

            "E1 slot filled",

        )

        self.assertEqual(

            Booking.objects.filter(slot=self.slot2).count(), 1,

            "E2 slot filled",

        )



        # Session should be completed

        self.session.refresh_from_db()

        self.assertEqual(self.session.status, "completed",

                         "Session completed when all expired ExamDates fully assigned")





# ---------------------------------------------------------------------------

# 18. Duplicate scheduler job prevention

# ---------------------------------------------------------------------------



class DuplicateJobPreventionTest(TestCase):

    """schedule_auto_assignment must not create duplicate jobs for same (session, exam_date)."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )



    def test_no_duplicate_jobs_after_repeated_scheduling(self):

        """Scheduling same (session, exam_date) twice yields exactly one job."""

        DjangoJob.objects.all().delete()

        now = timezone.now()

        schedule_auto_assignment(now, self.session.id, self.exam.id)

        schedule_auto_assignment(now, self.session.id, self.exam.id)

        schedule_auto_assignment(now, self.session.id, self.exam.id)



        jobs = scheduler.get_jobs()

        matching = [j for j in jobs if j.id == f'auto_assign_duties_{self.session.id}_{self.exam.id}']

        self.assertEqual(len(matching), 1,

                         "Exactly one job for repeated scheduling of same ExamDate")



    def test_different_exam_dates_have_different_jobs(self):

        """Different ExamDates in same session get different job IDs."""

        DjangoJob.objects.all().delete()

        exam2 = make_exam_date(

            self.session, days_offset=2, deadline_offset_hours=-1,

        )

        now = timezone.now()

        schedule_auto_assignment(now, self.session.id, self.exam.id)

        schedule_auto_assignment(now, self.session.id, exam2.id)



        jobs = scheduler.get_jobs()

        id1 = f'auto_assign_duties_{self.session.id}_{self.exam.id}'

        id2 = f'auto_assign_duties_{self.session.id}_{exam2.id}'

        matching1 = [j for j in jobs if j.id == id1]

        matching2 = [j for j in jobs if j.id == id2]

        self.assertEqual(len(matching1), 1, "Exactly one job for exam1")

        self.assertEqual(len(matching2), 1, "Exactly one job for exam2")

        self.assertNotEqual(id1, id2, "Job IDs are different")





# ---------------------------------------------------------------------------

# 19. Restart recovery with overdue and future jobs

# ---------------------------------------------------------------------------



class RestartRecoveryTest(TestCase):

    """start() recovers overdue work and preserves future deadlines."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user = make_user("rr1")

        self.teacher = make_teacher(

            self.user, "RR001", "Recovery Teacher",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam_past = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-5,

        )

        self.exam_future = make_exam_date(

            self.session, days_offset=5, deadline_offset_hours=24,

        )

        self.slot_past = make_slot(self.exam_past, shift="Morning", total_positions=2)

        self.slot_future = make_slot(self.exam_future, shift="Morning", total_positions=2)



    def test_overdue_work_recovered_on_start(self):

        """start() discovers overdue sessions even without prior scheduler jobs."""

        DjangoJob.objects.all().delete()



        start()



        # Check that overdue jobs are scheduled

        jobs = scheduler.get_jobs()

        overdue_jobs = [

            j for j in jobs if f'{self.session.id}_{self.exam_past.id}' in j.id

        ]

        self.assertGreater(

            len(overdue_jobs), 0, "Overdue job scheduled on start"

        )



        # Future slot should not have a job

        future_jobs = [

            j for j in jobs if f'{self.session.id}_{self.exam_future.id}' in j.id

        ]

        self.assertEqual(

            len(future_jobs), 0, "Future job not scheduled on start"

        )



        try:

            scheduler.shutdown(wait=True)

        except Exception:

            pass





# ---------------------------------------------------------------------------

# 20. Legacy scheduler job handling

# ---------------------------------------------------------------------------



class LegacyJobTest(TestCase):

    """Legacy auto_assign_duties job (no session/exam_date scoping) is cleaned up."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")



    def test_legacy_job_removed(self):

        """Legacy job with id='auto_assign_duties' is deleted on start()."""

        DjangoJob.objects.create(

            id='auto_assign_duties',

            next_run_time=timezone.now(),

            job_state=b'',

        )



        DjangoJob.objects.all().delete()  # clear any auto_assign_duties_* jobs

        # Re-add just the legacy one

        DjangoJob.objects.create(

            id='auto_assign_duties',

            next_run_time=timezone.now(),

            job_state=b'',

        )



        start()



        self.assertFalse(

            DjangoJob.objects.filter(id='auto_assign_duties').exists(),

            "Legacy job removed",

        )



        try:

            scheduler.shutdown(wait=True)

        except Exception:

            pass







# ---------------------------------------------------------------------------

# 21. Deadline execution via run_scheduler --once

# ---------------------------------------------------------------------------



class DeadlineExecutionTest(TestCase):

    """run_scheduler --once processes overdue ExamDates."""



    def setUp(self):
        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user = make_user("dl1")

        self.teacher = make_teacher(

            self.user, "DL001", "Deadline Teacher",

            role="permanent", final_duties=5, department=self.dept,

        )

        # Past deadline

        self.exam_past = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-5,

        )

        self.slot_past = make_slot(self.exam_past, shift="Morning", total_positions=2)

        # Future deadline

        self.exam_future = make_exam_date(

            self.session, days_offset=5, deadline_offset_hours=24,

        )

        self.slot_future = make_slot(self.exam_future, shift="Morning", total_positions=2)



    def test_overdue_examdate_processed_by_run_scheduler_once(self):

        """--once processes overdue ExamDate and assigns duties."""

        from django.core.management import call_command

        call_command("run_scheduler", "--once")

        # Overdue slot should have bookings

        bookings = Booking.objects.filter(slot=self.slot_past)

        self.assertGreater(

            bookings.count(), 0,

            "Overdue ExamDate should have been assigned duties",

        )



        # Future slot should have no bookings

        self.assertEqual(

            Booking.objects.filter(slot=self.slot_future).count(), 0,

            "Future ExamDate should not be assigned yet",

        )



        # Session status depends on whether all expired exams are filled

        # Since we only processed E1 (past) and E2 (future) is not yet due,

        # session should NOT be completed (future deadline remains)

        self.session.refresh_from_db()

        self.assertEqual(

            self.session.status, "active",

            "Session stays active when future deadline remains",

        )





# ---------------------------------------------------------------------------

# 22. Restart recovery with repeated scheduler start

# ---------------------------------------------------------------------------



class RestartRecoveryTest(TestCase):

    """Scheduler restart recovers overdue work without duplication."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.user = make_user("rr1")

        self.teacher = make_teacher(

            self.user, "RR001", "Recovery Teacher",

            role="permanent", final_duties=5, department=self.dept,

        )

        self.exam = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-5,

        )

        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)



    def test_restart_does_not_duplicate_bookings(self):

        """Running auto-assignment twice for same overdue ExamDate is idempotent."""

        from django.core.management import call_command



        # First run

        call_command("run_scheduler", "--once")

        bookings_after_first = Booking.objects.filter(slot=self.slot).count()



        # Second run (simulates restart)

        call_command("run_scheduler", "--once")

        bookings_after_second = Booking.objects.filter(slot=self.slot).count()



        # Should not have duplicated

        self.assertEqual(

            bookings_after_first, bookings_after_second,

            "Repeated auto-assignment must not create duplicate bookings",

        )



    def test_restart_recovers_overdue_deadline(self):

        """start() discovers and processes overdue sessions after restart."""

        from django.core.management import call_command



        # Clear all jobs first

        DjangoJob.objects.all().delete()



        # No bookings yet

        self.assertEqual(

            Booking.objects.filter(slot=self.slot).count(), 0,

            "No bookings before restart recovery",

        )



        # Simulate scheduler restart

        call_command("run_scheduler", "--once")



        # Overdue slot should now have bookings

        self.assertGreater(

            Booking.objects.filter(slot=self.slot).count(), 0,

            "Overdue ExamDate should be processed on restart",

        )





# ---------------------------------------------------------------------------

# 23. Unique scheduler job IDs per Session and ExamDate

# ---------------------------------------------------------------------------



class UniqueJobIdTest(TestCase):

    """Each (session_id, exam_date_id) pair gets exactly one scheduler job."""



    def setUp(self):

        self.dept = make_department()

        self.session = make_session(status="active", semester_type="odd")

        self.exam1 = make_exam_date(

            self.session, days_offset=1, deadline_offset_hours=-1,

        )

        self.exam2 = make_exam_date(

            self.session, days_offset=2, deadline_offset_hours=-1,

        )



    def test_unique_job_id_per_exam_date(self):

        """Different ExamDates in same session have different job IDs."""

        from django.core.management import call_command



        DjangoJob.objects.all().delete()

        now = timezone.now()



        schedule_auto_assignment(now, self.session.id, self.exam1.id)

        schedule_auto_assignment(now, self.session.id, self.exam2.id)



        jobs = scheduler.get_jobs()

        id1 = f'auto_assign_duties_{self.session.id}_{self.exam1.id}'

        id2 = f'auto_assign_duties_{self.session.id}_{self.exam2.id}'

        matching1 = [j for j in jobs if j.id == id1]

        matching2 = [j for j in jobs if j.id == id2]

        self.assertEqual(len(matching1), 1, "Exactly one job for exam1")

        self.assertEqual(len(matching2), 1, "Exactly one job for exam2")

        self.assertNotEqual(id1, id2, "Job IDs are different")



    def test_repeated_scheduling_single_job(self):

        """Scheduling same ExamDate twice yields exactly one job."""

        from django.core.management import call_command



        DjangoJob.objects.all().delete()

        now = timezone.now()



        schedule_auto_assignment(now, self.session.id, self.exam1.id)

        schedule_auto_assignment(now, self.session.id, self.exam1.id)



        jobs = scheduler.get_jobs()

        job_id = f'auto_assign_duties_{self.session.id}_{self.exam1.id}'

        matching = [j for j in jobs if j.id == job_id]

        self.assertEqual(len(matching), 1, "Exactly one job for repeated scheduling")



# ---------------------------------------------------------------------------
# 24. Recovery swap: fills vacancy via teacher swap (same quota pool)
# ---------------------------------------------------------------------------
#
# Scenario: two expired ExamDates on the same date (odd semester, all
# before-vacation, using final_duties).
#
# Slot X: Morning, total_positions=2, filled_positions=1 (Teacher A manual)
# Slot Y: Evening, total_positions=1, filled_positions=1 (Teacher B auto)
#
# Teacher A: final_duties=1, already has Morning duty on ExamDate (Slot X)
#   → Cannot take Slot X again (already booked), but CAN take Slot Y
#     (Evening ≠ Morning, day count 1→2 ≤ 2)
#
# Teacher B: final_duties=0, has auto-assigned booking in Slot Y
#   → Cannot take Slot X directly (no quota)
#   → After releasing Slot Y: final_duties=1, can take Slot X
#
# Swap: Teacher A → Slot Y (consumes final_duties 1→0)
#       Teacher B → Slot X (releases Slot Y +1, takes Slot X -1 = net 0)
# Result: Slot X filled (2/2), Slot Y filled (1/1), no manual booking moved.
#

class SwapRecoverySamePoolTest(TestCase):
    """Same-pool swap: Teacher A takes Slot Y, Teacher B takes Slot X."""

    def setUp(self):
        self.dept = make_department()
        self.session = make_session(status="active", semester_type="odd")

        # Single ExamDate, expired deadline
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )

        # Slot X: Morning, total_positions=2, filled_positions=1 (Teacher A manual)
        self.slot_x = make_slot(self.exam, shift="Morning", total_positions=2)
        self.slot_x.filled_positions = 1
        self.slot_x.save()

        # Slot Y: Evening, total_positions=1, filled_positions=1 (Teacher B auto)
        self.slot_y = make_slot(self.exam, shift="Evening", total_positions=1)
        self.slot_y.filled_positions = 1
        self.slot_y.save()

        # Teacher A: final_duties=1, already has manual booking in Slot X
        self.user_a = make_user("swap_a")
        self.teacher_a = make_teacher(
            self.user_a, "SWAPA01", "Swap Teacher A",
            role="permanent", final_duties=1, department=self.dept,
        )
        Booking.objects.create(
            teacher=self.teacher_a,
            slot=self.slot_x,
            auto_assigned=False,
        )

        # Teacher B: final_duties=0, has auto-assigned booking in Slot Y
        self.user_b = make_user("swap_b")
        self.teacher_b = make_teacher(
            self.user_b, "SWAPB01", "Swap Teacher B",
            role="permanent", final_duties=0, department=self.dept,
        )
        self.booking_b = Booking.objects.create(
            teacher=self.teacher_b,
            slot=self.slot_y,
            auto_assigned=True
        )

    def test_swap_fills_vacancy(self):
        """Valid cross-teacher swap fills the vacancy without violating constraints."""

        auto_assign_duties(session_id=self.session.id)

        # Slot X should now be fully filled (Teacher A manual + Teacher B auto)
        self.slot_x.refresh_from_db()
        self.assertEqual(
            self.slot_x.filled_positions, 2,
            "Slot X should be fully filled after swap recovery",
        )
        bx = Booking.objects.filter(slot=self.slot_x)
        self.assertEqual(bx.count(), 2)
        # One manual (Teacher A), one auto (Teacher B)
        self.assertEqual(bx.filter(auto_assigned=False).count(), 1)
        self.assertEqual(bx.filter(auto_assigned=True).count(), 1)
        self.assertTrue(
            bx.filter(teacher=self.teacher_a, auto_assigned=False).exists(),
            "Teacher A manual booking should remain",
        )
        self.assertTrue(
            bx.filter(teacher=self.teacher_b, auto_assigned=True).exists(),
            "Teacher B auto booking should be in Slot X",
        )

        # Slot Y should still be filled (now by Teacher A)
        self.slot_y.refresh_from_db()
        self.assertEqual(
            self.slot_y.filled_positions, 1,
            "Slot Y should remain filled",
        )
        by = Booking.objects.filter(slot=self.slot_y)
        self.assertEqual(by.count(), 1)
        self.assertTrue(
            by.filter(teacher=self.teacher_a, auto_assigned=True).exists(),
            "Teacher A should have taken Slot Y",
        )

        # Duty counters: Teacher A used final_duties (1→0)
        self.teacher_a.refresh_from_db()
        self.assertEqual(
            self.teacher_a.final_duties, 0,
            "Teacher A counter should decrease by 1",
        )

        # Teacher B: released Slot Y (+1), took Slot X (-1) = net 0
        self.teacher_b.refresh_from_db()
        self.assertEqual(
            self.teacher_b.final_duties, 0,
            "Teacher B counter should be unchanged (net 0)",
        )

    def test_manual_booking_not_moved(self):
        """Manual bookings must never be moved by recovery swap."""

        auto_assign_duties(session_id=self.session.id)

        # Teacher A's manual booking in Slot X must remain
        self.assertTrue(
            Booking.objects.filter(
                teacher=self.teacher_a,
                slot=self.slot_x,
                auto_assigned=False,
            ).exists(),
            "Teacher A manual booking must not be moved",
        )

        # Teacher B's auto-assigned booking moved from Slot Y to Slot X
        self.assertTrue(
            Booking.objects.filter(
                teacher=self.teacher_b,
                slot=self.slot_x,
                auto_assigned=True,
            ).exists(),
            "Teacher B should be in Slot X after swap",
        )

        # Teacher A should now be in Slot Y (auto-assigned)
        self.assertTrue(
            Booking.objects.filter(
                teacher=self.teacher_a,
                slot=self.slot_y,
                auto_assigned=True,
            ).exists(),
            "Teacher A should be in Slot Y after swap",
        )

    def test_no_swap_when_no_eligible_teachers(self):
        """No swap when no teacher has remaining quota for Slot Y."""

        # Teacher A has no remaining quota
        self.teacher_a.final_duties = 0
        self.teacher_a.save()

        auto_assign_duties(session_id=self.session.id)

        # Slot X should remain vacant (1/2)
        self.slot_x.refresh_from_db()
        self.assertEqual(
            self.slot_x.filled_positions, 1,
            "Slot X should remain 1/2 when no teacher can fill it",
        )

        # Slot Y should remain with Teacher B
        self.assertEqual(
            Booking.objects.filter(
                teacher=self.teacher_b, slot=self.slot_y
            ).count(), 1,
            "Teacher B should remain in Slot Y",
        )

    def test_manual_booking_in_slot_y_not_moved(self):
        """A Booking changed to manual must not be moved by recovery swap."""

        # Change Teacher B's auto-assigned Booking at Slot Y to manual
        self.booking_b.auto_assigned = False
        self.booking_b.save()

        auto_assign_duties(session_id=self.session.id)

        # Slot X should remain vacant (1/2) - Teacher B's manual booking
        # at Slot Y cannot be moved, so Teacher B can't take Slot X
        self.slot_x.refresh_from_db()
        self.assertEqual(
            self.slot_x.filled_positions, 1,
            "Slot X should remain 1/2 when Teacher B's manual booking "
            "can't be moved out of Slot Y",
        )

        # Teacher B should still be in Slot Y (manual booking untouched)
        self.assertTrue(
            Booking.objects.filter(
                teacher=self.teacher_b,
                slot=self.slot_y,
                auto_assigned=False,
            ).exists(),
            "Teacher B's manual Booking at Slot Y must not be moved",
        )

        # Teacher A should NOT have taken Slot Y
        self.assertFalse(
            Booking.objects.filter(
                teacher=self.teacher_a,
                slot=self.slot_y,
            ).exists(),
            "Teacher A should not take Slot Y when Teacher B's manual "
            "booking can't be moved out",
        )
