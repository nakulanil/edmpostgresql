"""Guard against destructive session setup uploads after teachers have booked."""

from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from teachers.admin import SessionAdmin
from teachers.models import Booking, ExamDate, Session, Slot, Teacher


class SessionReuploadProtectionTest(TestCase):
    def test_reupload_with_existing_booking_is_rejected_before_mutation(self):
        session = Session.objects.create(
            name="Active booked session",
            status="active",
            semester_type="odd",
            room_capacity=50,
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.localdate() + timedelta(days=4),
            booking_deadline=timezone.now() + timedelta(days=2),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        slot = Slot.objects.create(
            exam_date=exam_date, shift="Morning",
            total_positions=2, filled_positions=1,
        )
        teacher = Teacher.objects.create(
            user=User.objects.create_user(username="booked-teacher"),
            faculty_id="BOOKED001",
            full_name="Booked Teacher",
            final_duties=2,
        )
        booking = Booking.objects.create(
            slot=slot, teacher=teacher, auto_assigned=False
        )

        request = RequestFactory().post(
            f"/admin/teachers/session/{session.pk}/setup/",
            data={"room_capacity": "100", "semester_type": "even"},
        )
        request.user = User.objects.create_superuser(
            username="setup-admin", password="test-password", email="admin@example.test"
        )
        admin = SessionAdmin(Session, AdminSite())

        with patch.object(admin, "message_user") as message_user:
            response = admin.setup_session_view(request, session.pk)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(message_user.called)
        self.assertIn("locked", message_user.call_args.args[1].lower())
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())
        slot.refresh_from_db()
        teacher.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(slot.filled_positions, 1)
        self.assertEqual(teacher.final_duties, 2)
        self.assertEqual(session.room_capacity, 50)
        self.assertEqual(session.semester_type, "odd")
