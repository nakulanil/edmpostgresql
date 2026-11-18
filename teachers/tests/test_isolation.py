"""
Test isolation verification for Exam Duty Manager.

These tests verify that:
1. Django can initialize without starting APScheduler during tests.
2. Basic model creation works in the isolated test database.
3. The test database is not the production db.sqlite3.
"""

import os
import sys
from django.test import TestCase, TransactionTestCase
from django.db import connection, transaction
from django.contrib.auth.models import User

from teachers.models import Session, ExamDate, Slot, Teacher, Department, Booking
from django.utils import timezone
from datetime import timedelta, time


class SchedulerIsolationTest(TestCase):
    """Verify APScheduler does not start during test execution."""

    def test_testing_env_var_set_before_django_setup(self):
        """TESTING=1 must be set before Django initializes."""
        self.assertEqual(os.environ.get('TESTING'), '1')

    def test_scheduler_not_started(self):
        """APScheduler BackgroundScheduler should not be running during tests."""
        from teachers.scheduler import scheduler
        self.assertFalse(scheduler.running, "Scheduler should not be running in test mode")

    def test_apps_ready_skipped_scheduler(self):
        """TeachersConfig.ready() should skip scheduler startup when TESTING=1."""
        from teachers.apps import TeachersConfig
        from django.apps import apps
        config = apps.get_app_config('teachers')
        self.assertIsInstance(config, TeachersConfig)
        # Calling ready() again should not start scheduler
        config.ready()
        from teachers.scheduler import scheduler
        self.assertFalse(scheduler.running)

    def test_apps_ready_skipped_without_scheduler_enabled(self):
        """TeachersConfig.ready() should skip scheduler when SCHEDULER_ENABLED is not set."""
        from teachers.apps import TeachersConfig
        from django.apps import apps
        config = apps.get_app_config('teachers')
        self.assertIsInstance(config, TeachersConfig)
        # Temporarily unset SCHEDULER_ENABLED to simulate management command
        old_val = os.environ.get('SCHEDULER_ENABLED')
        os.environ.pop('SCHEDULER_ENABLED', None)
        try:
            config.ready()
            from teachers.scheduler import scheduler
            self.assertFalse(scheduler.running)
        finally:
            if old_val is not None:
                os.environ['SCHEDULER_ENABLED'] = old_val
            else:
                os.environ.pop('SCHEDULER_ENABLED', None)


class TestDatabaseIsolationTest(TestCase):
    """Verify test database isolation from production."""

    def test_test_database_is_not_production(self):
        """Test database must not be the production database (examduty_db)."""
        db_name = connection.settings_dict['NAME']
        
        # For SQLite, ensure we don't use the real project database files
        if connection.vendor == 'sqlite':
            self.assertNotEqual(db_name, 'db.sqlite3')
            self.assertNotEqual(db_name, '/home/niko/Documents/examduty/db.sqlite3')
            self.assertNotIn('db.sqlite3', str(db_name))
        
        # For PostgreSQL, ensure we don't access the production database
        elif connection.vendor == 'postgresql':
            self.assertNotEqual(db_name, 'examduty_db')
        
        else:
            raise NotImplementedError(f"Unsupported database vendor: {connection.vendor}")

    def test_test_database_is_safe_to_reset(self):
        """Test database must be a dedicated test database that can be safely reset."""
        db_name = connection.settings_dict['NAME']
        
        # For SQLite, test databases use test_ prefix
        if connection.vendor == 'sqlite':
            # SQLite test databases should have 'test_' prefix
            self.assertTrue(
                db_name.startswith('test_') or '/test_' in db_name,
                f"SQLite test database should start with 'test_', got: {db_name}"
            )
            # Ensure we don't use the project's real SQLite database
            self.assertNotIn('db.sqlite3', str(db_name))
        
        # For PostgreSQL, test databases must be explicitly created as test databases
        elif connection.vendor == 'postgresql':
            # PostgreSQL test database must not be the production database
            self.assertNotEqual(db_name, 'examduty_db')
            
            # Check if this is a test database by examining the naming pattern
            # Test databases should be explicitly created as test databases
            # We verify by ensuring this is NOT the production database
            self.assertTrue(
                'examduty_test' in db_name,
                f"PostgreSQL test database must contain 'examduty_test', got: {db_name}"
            )
        
        else:
            raise NotImplementedError(f"Unsupported database vendor: {connection.vendor}")

    def test_backend_compatibility(self):
        """Test that the test database is compatible with the expected backend."""
        db_name = connection.settings_dict['NAME']
        
        # Verify database backend matches Django configuration
        if connection.vendor == 'sqlite':
            # SQLite-specific validations
            pass  # Additional SQLite-specific checks could go here
        
        elif connection.vendor == 'postgresql':
            # PostgreSQL-specific validations
            pass  # Additional PostgreSQL-specific checks could go here
        
        else:
            raise NotImplementedError(f"Unsupported database vendor: {connection.vendor}")


class ModelCreationTest(TestCase):
    """Basic model creation tests using verified model fields."""

    def setUp(self):
        self.department = Department.objects.create(name="Computer Science")
        self.user = User.objects.create_user(username='testuser', password='testpass123')

    def test_department_creation(self):
        dept = Department.objects.create(name="Mathematics")
        self.assertEqual(dept.name, "Mathematics")
        self.assertTrue(Department.objects.filter(name="Mathematics").exists())

    def test_session_creation(self):
        session = Session.objects.create(
            name="Test Session 2026",
            status='active',
            semester_type='odd',
            minimum_duty=1,
            room_capacity=50,
        )
        self.assertEqual(session.name, "Test Session 2026")
        self.assertEqual(session.status, 'active')
        self.assertEqual(session.semester_type, 'odd')
        self.assertEqual(session.minimum_duty, 1)
        self.assertEqual(session.room_capacity, 50)
        self.assertIsNotNone(session.created_at)

    def test_session_status_choices(self):
        for status_val, _ in Session.STATUS_CHOICES:
            session = Session.objects.create(
                name=f"Session {status_val}",
                status=status_val,
                semester_type='odd',
            )
            self.assertEqual(session.status, status_val)

    def test_session_semester_type_choices(self):
        for sem_val, _ in Session.SEMESTER_CHOICES:
            session = Session.objects.create(
                name=f"Session {sem_val}",
                status='upcoming',
                semester_type=sem_val,
            )
            self.assertEqual(session.semester_type, sem_val)

    def test_exam_date_creation(self):
        session = Session.objects.create(
            name="Test Session",
            status='active',
            semester_type='odd',
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=7),
            booking_deadline=timezone.now() + timedelta(days=6),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
            total_students=100,
        )
        self.assertEqual(exam_date.session, session)
        self.assertEqual(exam_date.total_students, 100)

    def test_slot_creation(self):
        session = Session.objects.create(
            name="Test Session",
            status='active',
            semester_type='odd',
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=7),
            booking_deadline=timezone.now() + timedelta(days=6),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        slot = Slot.objects.create(
            exam_date=exam_date,
            shift='Morning',
            total_positions=5,
            filled_positions=0,
        )
        self.assertEqual(slot.exam_date, exam_date)
        self.assertEqual(slot.shift, 'Morning')
        self.assertEqual(slot.total_positions, 5)
        self.assertEqual(slot.filled_positions, 0)

    def test_slot_unique_together(self):
        session = Session.objects.create(
            name="Test Session",
            status='active',
            semester_type='odd',
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=7),
            booking_deadline=timezone.now() + timedelta(days=6),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        Slot.objects.create(
            exam_date=exam_date,
            shift='Morning',
            total_positions=5,
            filled_positions=0,
        )
        with self.assertRaises(Exception):
            Slot.objects.create(
                exam_date=exam_date,
                shift='Morning',
                total_positions=3,
                filled_positions=0,
            )

    def test_teacher_creation(self):
        teacher = Teacher.objects.create(
            user=self.user,
            faculty_id='T001',
            full_name='Test Teacher',
            email='test@example.com',
            phone_number='1234567890',
            department=self.department,
            role='permanent',
            allocated_duties=0,
            relaxed_duties=0,
            final_duties=3,
            after_vacation_duties=0,
        )
        self.assertEqual(teacher.faculty_id, 'T001')
        self.assertEqual(teacher.full_name, 'Test Teacher')
        self.assertEqual(teacher.role, 'permanent')
        self.assertEqual(teacher.final_duties, 3)
        self.assertEqual(teacher.after_vacation_duties, 0)

    def test_teacher_role_choices(self):
        for role_val, _ in Teacher.ROLE_CHOICES:
            user = User.objects.create_user(username=f'user_{role_val}', password='pass')
            teacher = Teacher.objects.create(
                user=user,
                faculty_id=f'FAC_{role_val}',
                full_name=f'Teacher {role_val}',
                role=role_val,
            )
            self.assertEqual(teacher.role, role_val)

    def test_booking_creation(self):
        session = Session.objects.create(
            name="Test Session",
            status='active',
            semester_type='odd',
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=7),
            booking_deadline=timezone.now() + timedelta(days=6),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        slot = Slot.objects.create(
            exam_date=exam_date,
            shift='Morning',
            total_positions=5,
            filled_positions=0,
        )
        teacher = Teacher.objects.create(
            user=self.user,
            faculty_id='T001',
            full_name='Test Teacher',
            role='permanent',
            final_duties=3,
        )
        booking = Booking.objects.create(
            teacher=teacher,
            slot=slot,
            auto_assigned=False,
        )
        self.assertEqual(booking.teacher, teacher)
        self.assertEqual(booking.slot, slot)
        self.assertFalse(booking.auto_assigned)
        self.assertIsNotNone(booking.booked_at)

    def test_booking_auto_assigned_flag(self):
        session = Session.objects.create(
            name="Test Session",
            status='active',
            semester_type='odd',
        )
        exam_date = ExamDate.objects.create(
            session=session,
            date=timezone.now().date() + timedelta(days=7),
            booking_deadline=timezone.now() + timedelta(days=6),
            morning_start=time(9, 0),
            morning_end=time(12, 0),
            evening_start=time(14, 0),
            evening_end=time(17, 0),
        )
        slot = Slot.objects.create(
            exam_date=exam_date,
            shift='Morning',
            total_positions=5,
            filled_positions=0,
        )
        teacher = Teacher.objects.create(
            user=self.user,
            faculty_id='T001',
            full_name='Test Teacher',
            role='permanent',
            final_duties=3,
        )
        booking = Booking.objects.create(
            teacher=teacher,
            slot=slot,
            auto_assigned=True,
        )
        self.assertTrue(booking.auto_assigned)


class TransactionRollbackTest(TransactionTestCase):
    """Verify transaction rollback works in test database."""

    def test_transaction_rollback(self):
        """Data created in a transaction that rolls back should not persist."""
        dept = Department.objects.create(name="Physics")
        initial_count = Department.objects.count()

        try:
            with transaction.atomic():
                Department.objects.create(name="Chemistry")
                raise ValueError("Force rollback")
        except ValueError:
            pass

        self.assertEqual(Department.objects.count(), initial_count)