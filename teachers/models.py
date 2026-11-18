import math
from django.db import models
from django.contrib.auth.models import User


class Session(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('upcoming', 'Upcoming'),
        ('completed', 'Completed'),
    ]
    SEMESTER_CHOICES = [
        ('odd', 'Odd Semester'),
        ('even', 'Even Semester'),
    ]
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')
    semester_type = models.CharField(max_length=10, choices=SEMESTER_CHOICES, default='odd')
    vacation_start_date = models.DateField(null=True, blank=True,
        help_text="Only for Even semester — exam dates on or after this date are excluded from before-vacation duties.")
    guest_duties_per_teacher = models.IntegerField(null=True, blank=True,
        help_text="Only for Even semester — number of after-vacation duties per guest teacher (n).")
    minimum_duty = models.IntegerField(default=1,
        help_text="Floor for final_duties — no teacher gets fewer than this.")
    room_capacity = models.IntegerField(default=50,
        help_text="Number of students one exam room can hold. Used to calculate rooms & invigilator positions.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.status})"


class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Teacher(models.Model):
    ROLE_CHOICES = [
        ('deputy_superintendent', 'Deputy Superintendent'),
        ('adhoc', 'Adhoc'),
        ('permanent', 'Permanent'),
        ('guest', 'Guest Teacher'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    teacher_id = models.AutoField(primary_key=True)
    faculty_id = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=15, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    role = models.CharField(max_length=25, choices=ROLE_CHOICES, default='permanent')

    # Duty allocation fields — computed after session setup + practical duty upload
    allocated_duties = models.IntegerField(default=0)
    relaxed_duties = models.IntegerField(default=0)
    final_duties = models.IntegerField(default=0)
    after_vacation_duties = models.IntegerField(default=0,
        help_text="Duties for after-vacation period (even semester only). "
                  "Guest teachers: set to n; adhoc/permanent: their share of remaining.")

    # Kept for forgot-password flow
    otp = models.CharField(max_length=6, blank=True, null=True)
    otp_expiry = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.full_name


class ExamDate(models.Model):
    session = models.ForeignKey(
        Session, on_delete=models.CASCADE,
        related_name='exam_dates', null=True, blank=True
    )
    date = models.DateField()
    morning_start = models.TimeField()
    morning_end = models.TimeField()
    evening_start = models.TimeField()
    evening_end = models.TimeField()
    total_students = models.IntegerField(default=0)
    booking_deadline = models.DateTimeField()

    @property
    def rooms(self):
        morning = self.exams.filter(session='Morning').aggregate(
            t=models.Sum('students'))['t'] or 0
        evening = self.exams.filter(session='Evening').aggregate(
            t=models.Sum('students'))['t'] or 0
        total_rooms = 0
        capacity = self.session.room_capacity if self.session else 50
        if morning > 0:
            total_rooms += math.ceil(morning / capacity)
        if evening > 0:
            total_rooms += math.ceil(evening / capacity)
        return total_rooms

    def slots_count(self):
        return self.slots.count()
    slots_count.short_description = "Slots"

    def __str__(self):
        return f"{self.date}"


class Exam(models.Model):
    exam_date = models.ForeignKey(ExamDate, on_delete=models.CASCADE, related_name='exams')
    exam_name = models.CharField(max_length=255)

    students = models.IntegerField()
    session = models.CharField(max_length=10)  # 'Morning' or 'Evening'

    def __str__(self):
        return f"{self.exam_name} ({self.session})"


class Slot(models.Model):
    exam_date = models.ForeignKey(ExamDate, related_name='slots', on_delete=models.CASCADE)
    shift = models.CharField(max_length=10)
    total_positions = models.IntegerField()
    filled_positions = models.IntegerField(default=0)

    @property
    def shift_students(self):
        return self.exam_date.exams.filter(session=self.shift).aggregate(
            total=models.Sum('students')
        )['total'] or 0

    class Meta:
        unique_together = ('exam_date', 'shift')

    def __str__(self):
        return f"{self.exam_date} - {self.shift}"


class Booking(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    slot = models.ForeignKey(Slot, on_delete=models.CASCADE)
    booked_at = models.DateTimeField(auto_now_add=True)
    auto_assigned = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.teacher} booked {self.slot}"


class Notification(models.Model):
    ACTIVITY_CHOICES = [
        ('booked', 'Slot Booked'),
        ('cancelled', 'Slot Cancelled'),
        ('auto_assigned', 'Auto Assigned'),
    ]
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='notifications')
    activity = models.CharField(max_length=20, choices=ACTIVITY_CHOICES)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.teacher} - {self.activity} - {self.created_at}"


class PracticalExamDuty(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='practical_duties')
    teacher_name = models.CharField(max_length=255)
    faculty_id = models.CharField(max_length=50)
    duty_hours = models.IntegerField(default=0)

    class Meta:
        verbose_name = "Practical Exam Duty"
        verbose_name_plural = "Practical Exam Duties"

    def __str__(self):
        return f"{self.teacher_name} ({self.faculty_id}) — {self.duty_hours}h [Session {self.session_id}]"



