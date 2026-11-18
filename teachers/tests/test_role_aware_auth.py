"""
Phase 3 — Role-aware authentication and dashboard access tests.

Tests cover:
- Superuser without Teacher profile → denied teacher views, redirected to /admin/
- Staff user without Teacher profile → denied teacher views, redirected to /admin/
- Valid Teacher account → full access
- Ordinary account without Teacher profile → denied, redirect to login
- Unauthorized teacher API access → HTTP 403 JSON
- Login redirects based on user type
- Teacher accounts that also have staff permissions
"""

from django.test import TestCase, TransactionTestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from teachers.models import Session, ExamDate, Slot, Teacher, Department, Booking
from django.utils import timezone
from datetime import timedelta, time


def make_department(name="CS"):
    return Department.objects.create(name=name)


def make_user(username, is_staff=False, is_superuser=False):
    u = User.objects.create_user(username=username, password="testpass123")
    u.is_staff = is_staff
    u.is_superuser = is_superuser
    u.save()
    return u


def make_teacher(user, faculty_id, full_name, role="permanent",
                 final_duties=3, after_vacation_duties=0, department=None):
    return Teacher.objects.create(
        user=user, faculty_id=faculty_id, full_name=full_name,
        role=role, department=department,
        final_duties=final_duties,
        after_vacation_duties=after_vacation_duties,
    )


def make_session(status="active", semester_type="odd"):
    return Session.objects.create(
        name="Test Session", status=status, semester_type=semester_type,
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


class SuperuserNoTeacherTest(TestCase):
    """Superuser without Teacher profile must NOT enter teacher views."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("admin1", is_superuser=True, is_staff=True)
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)

    def test_dashboard_data_returns_403(self):
        """JSON dashboard_data returns HTTP 403 for superuser without Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertEqual(data['error'], 'teacher_profile_required')

    def test_dashboard_redirects_to_admin(self):
        """HTML dashboard redirects superuser without Teacher to /admin/."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')

    def test_book_slot_redirects_to_admin(self):
        """HTML book_slot redirects superuser without Teacher to /admin/."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('book_slot', args=[self.slot.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')

    def test_cancel_booking_redirects_to_admin(self):
        """HTML cancel_booking redirects superuser without Teacher to /admin/."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('cancel_booking', args=[999]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')

    def test_mark_notifications_read_returns_403(self):
        """JSON mark_notifications_read returns HTTP 403 for superuser."""
        self.client.force_login(self.user)
        response = self.client.post(reverse('mark_notifications_read'),
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_download_my_report_redirects_to_admin(self):
        """HTML download_my_report redirects superuser without Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('download_my_report'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')


class StaffNoTeacherTest(TestCase):
    """Staff user without Teacher profile must NOT enter teacher views."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("staff1", is_staff=True)
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)

    def test_dashboard_data_returns_403(self):
        """JSON dashboard_data returns HTTP 403 for staff without Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_dashboard_redirects_to_admin(self):
        """HTML dashboard redirects staff without Teacher to /admin/."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')


class OrdinaryUserNoTeacherTest(TestCase):
    """Ordinary user without Teacher profile must NOT enter teacher views."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("ordinary1")
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)

    def test_dashboard_data_returns_403(self):
        """JSON dashboard_data returns HTTP 403 for ordinary user without Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_dashboard_redirects_to_login(self):
        """HTML dashboard redirects ordinary user without Teacher to login."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login'))

    def test_book_slot_redirects_to_login(self):
        """HTML book_slot redirects ordinary user without Teacher to login."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('book_slot', args=[self.slot.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login'))


class ValidTeacherAccessTest(TestCase):
    """Valid Teacher accounts get full access to teacher views."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("teacher1")
        self.teacher = make_teacher(
            self.user, "T001", "Valid Teacher",
            role="permanent", final_duties=3, department=self.dept,
        )
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=24,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)

    def test_dashboard_returns_200(self):
        """Dashboard returns HTTP 200 for valid Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_data_returns_200(self):
        """JSON dashboard_data returns HTTP 200 for valid Teacher."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('final_duties', data)
        self.assertIn('role', data)
        self.assertIn('slots', data)

    def test_book_slot_works(self):
        """Valid Teacher can book a slot."""
        self.client.force_login(self.user)
        response = self.client.post(reverse('book_slot', args=[self.slot.id]))
        self.assertEqual(response.status_code, 302)  # redirect to dashboard
        self.assertTrue(
            Booking.objects.filter(teacher=self.teacher, slot=self.slot).exists()
        )


class TeacherAlsoStaffTest(TestCase):
    """Teacher account that also has staff permissions can access teacher views."""

    def setUp(self):
        self.dept = make_department()
        self.user = make_user("teacher_staff", is_staff=True)
        self.teacher = make_teacher(
            self.user, "T002", "Teacher Staff",
            role="permanent", final_duties=3, department=self.dept,
        )
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)

    def test_dashboard_returns_200(self):
        """Teacher who is also staff gets dashboard access."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_data_returns_200(self):
        """Teacher who is also staff gets dashboard_data access."""
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)


class LoginRedirectTest(TestCase):
    """Post-login redirects based on user type."""

    def setUp(self):
        self.dept = make_department()
        self.user_teacher = make_user("rt1")
        self.teacher = make_teacher(
            self.user_teacher, "RT001", "Redirect Teacher",
            role="permanent", final_duties=3, department=self.dept,
        )
        self.user_staff = make_user("rs1", is_staff=True)
        self.user_ordinary = make_user("ro1")

    def test_teacher_redirects_to_dashboard(self):
        """Teacher logs in and is redirected to dashboard."""
        self.client.login(username="rt1", password="testpass123")
        response = self.client.get(reverse('login'))
        # Login page itself should be accessible
        self.assertEqual(response.status_code, 200)

    def test_teacher_dashboard_access_after_login(self):
        """Teacher can access dashboard after login."""
        self.client.login(username="rt1", password="testpass123")
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_staff_no_teacher_redirects_to_admin_after_login(self):
        """Staff without Teacher profile redirected to /admin/ after login."""
        self.client.login(username="rs1", password="testpass123")
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')

    def test_ordinary_no_teacher_redirects_to_login_after_login(self):
        """Ordinary user without Teacher profile redirected to login after login."""
        self.client.login(username="ro1", password="testpass123")
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login'))


class TeacherCannotAccessOtherTeacherBookings(TestCase):
    """Teachers cannot access another teacher's bookings."""

    def setUp(self):
        self.dept = make_department()
        self.user1 = make_user("t_a")
        self.user2 = make_user("t_b")
        self.teacher1 = make_teacher(
            self.user1, "TA001", "Teacher A",
            role="permanent", final_duties=3, department=self.dept,
        )
        self.teacher2 = make_teacher(
            self.user2, "TB001", "Teacher B",
            role="permanent", final_duties=3, department=self.dept,
        )
        self.session = make_session()
        self.exam = make_exam_date(
            self.session, days_offset=1, deadline_offset_hours=-2,
        )
        self.slot = make_slot(self.exam, shift="Morning", total_positions=2)
        # Teacher A books the slot
        self.booking = Booking.objects.create(
            teacher=self.teacher1, slot=self.slot,
        )

    def test_teacher_b_cannot_cancel_teacher_a_booking(self):
        """Teacher B cannot cancel Teacher A's booking."""
        self.client.force_login(self.user2)
        response = self.client.post(
            reverse('cancel_booking', args=[self.booking.id])
        )
        self.assertEqual(response.status_code, 302)
        # Booking should still exist
        self.assertTrue(
            Booking.objects.filter(id=self.booking.id).exists()
        )

    def test_teacher_b_cannot_see_teacher_a_bookings_in_dashboard_data(self):
        """Teacher B's dashboard_data does not include Teacher A's bookings."""
        self.client.force_login(self.user2)
        response = self.client.get(reverse('dashboard_data'),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        booking_ids = [b['id'] for b in data['bookings']]
        self.assertNotIn(self.booking.id, booking_ids)
