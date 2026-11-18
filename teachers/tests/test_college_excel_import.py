"""Synthetic Excel import smoke tests matching college upload column layouts.

No faculty names, emails or data from actual uploaded workbooks are stored here.
"""
from datetime import timedelta
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from teachers.models import Booking, Exam, ExamDate, Session, Slot, Teacher


def excel_file(name, headers, rows):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return SimpleUploadedFile(
        name, buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class CollegeExcelImportTest(TestCase):
    def setUp(self):
        admin = User.objects.create_superuser(
            username="excel-test-admin",
            email="excel-admin@example.test",
            password="test-admin-password",
        )
        self.client.force_login(admin)

    def test_teacher_bulk_upload_creates_accounts_and_reimport_updates(self):
        url = reverse("admin:teachers_teacher_bulk_upload")
        headers = [
            "Faculty ID", "Full Name", "Department", "Email", "Phone Number", "Role"
        ]
        rows = [
            ["SYN001", "Example One", "Computer Science", "one@example.test", "", "Permanent"],
            ["SYN002", "Example Two", "Computer Science", "two@example.test", "", "Adhoc"],
            ["SYN003", "Example Three", "Mathematics", "three@example.test", "", "Guest"],
            ["SYN004", "Example Four", "Mathematics", "four@example.test", "", "Deputy Superintendent"],
        ]
        response = self.client.post(url, {"xlsx_file": excel_file("faculty.xlsx", headers, rows)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Teacher.objects.count(), 4)
        self.assertEqual(Teacher.objects.get(faculty_id="SYN003").role, "guest")
        self.assertEqual(
            Teacher.objects.get(faculty_id="SYN004").role,
            "deputy_superintendent",
        )
        self.assertEqual(Teacher.objects.get(faculty_id="SYN001").department.name, "Computer Science")

        rows[0][3] = "updated@example.test"
        response = self.client.post(url, {"xlsx_file": excel_file("faculty.xlsx", headers, rows)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Teacher.objects.count(), 4)
        self.assertEqual(Teacher.objects.get(faculty_id="SYN001").email, "updated@example.test")

    def test_session_excel_upload_creates_exam_dates_slots_and_duty_quotas(self):
        Teacher.objects.create(
            user=User.objects.create_user(username="eligible-excel-teacher"),
            faculty_id="TEST-ELIGIBLE",
            full_name="Eligible Teacher",
            role="permanent",
        )
        session = Session.objects.create(
            name="Synthetic import test", status="upcoming", semester_type="odd"
        )
        exam_date = timezone.localdate() + timedelta(days=7)
        headers = ["Exam Name", "Exam Date", "Morning Students", "Evening Students"]
        rows = [
            ["Paper A", exam_date.isoformat(), 75, 0],
            ["Paper B", exam_date.isoformat(), 0, 25],
        ]
        url = reverse("admin:session_setup", args=[session.pk])
        deadline = timezone.localtime(timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            url,
            {
                "excel_file": excel_file("exams.xlsx", headers, rows),
                "booking_deadline": deadline,
                "semester_type": "odd",
                "minimum_duty": "1",
                "room_capacity": "50",
                "morning_start": "09:00",
                "morning_end": "12:00",
                "evening_start": "14:00",
                "evening_end": "17:00",
                "buffer": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.status, "active")
        dates = ExamDate.objects.filter(session=session)
        self.assertEqual(dates.count(), 1)
        self.assertEqual(Exam.objects.filter(exam_date__session=session).count(), 2)
        morning = Slot.objects.get(exam_date__session=session, shift="Morning")
        evening = Slot.objects.get(exam_date__session=session, shift="Evening")
        self.assertEqual(morning.total_positions, 4)  # ceil(75 / 50) * 2
        self.assertEqual(evening.total_positions, 2)  # ceil(25 / 50) * 2
        self.assertFalse(Booking.objects.filter(slot__exam_date__session=session).exists())
        teacher = Teacher.objects.get(faculty_id="TEST-ELIGIBLE")
        self.assertEqual(teacher.final_duties, 6)
