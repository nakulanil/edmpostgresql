import io
import math
import random
import openpyxl
from django.db import models as db_models
from django.template.response import TemplateResponse
from django.contrib import admin
from django.contrib import messages
from django.contrib.auth.models import User
from django.shortcuts import render, redirect
from django.urls import path
from django.utils.html import format_html
from django.utils.dateparse import parse_datetime
from django import forms
from .models import (
    Department, Exam, Teacher, Slot, ExamDate,
    Booking, Notification, Session, PracticalExamDuty,
)
from django.http import HttpResponse
from django.urls import reverse
from django.utils.safestring import mark_safe


def parse_xlsx(uploaded_file):
    """Read an uploaded .xlsx file and return a list of row dicts."""
    from datetime import datetime as _dt

    wb = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return []

    headers = [str(h or '').strip().lower().replace(' ', '_') for h in rows[0]]
    result = []
    for row in rows[1:]:
        # Pad short rows with None, trim long rows
        padded = list(row) + [None] * (len(headers) - len(row))
        row_dict = {}
        for header, cell in zip(headers, padded[:len(headers)]):
            if cell is None:
                row_dict[header] = ''
            elif isinstance(cell, _dt):
                row_dict[header] = cell.strftime('%Y-%m-%d')
            else:
                row_dict[header] = str(cell).strip()
        result.append(row_dict)

    return result


# ─── Department Admin ──────────────────────────────────────────────────────────

class TeacherInlineDepartment(admin.TabularInline):
    model = Teacher
    fields = ['faculty_id', 'full_name', 'email', 'phone_number', 'role', 'final_duties', 'after_vacation_duties']
    readonly_fields = ['faculty_id', 'full_name', 'email', 'phone_number', 'role', 'final_duties', 'after_vacation_duties']
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'teacher_count']
    search_fields = ['name']
    inlines = [TeacherInlineDepartment]

    def teacher_count(self, obj):
        return obj.teacher_set.count()
    teacher_count.short_description = 'Teachers'


# ─── Teacher Admin Forms ───────────────────────────────────────────────────────

class TeacherCreationForm(forms.ModelForm):
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(label='Password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm Password', widget=forms.PasswordInput)

    class Meta:
        model = Teacher
        fields = [
            'faculty_id',
            'full_name',
            'department',
            'email',
            'phone_number',
            'role',
        ]

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')

        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords don't match.")

        return cleaned_data

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already exists.")
        return username

    def clean_faculty_id(self):
        faculty_id = self.cleaned_data.get('faculty_id')
        if Teacher.objects.filter(faculty_id=faculty_id).exists():
            raise forms.ValidationError("Faculty ID already exists.")
        return faculty_id


class TeacherChangeForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = [
            'faculty_id',
            'full_name',
            'department',
            'email',
            'phone_number',
            'role',
        ]


# ─── Teacher Admin ─────────────────────────────────────────────────────────────

@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = [
        'faculty_id',
        'full_name',
        'email',
        'phone_number',
        'department',
        'role_badge',
        'allocated_duties',
        'relaxed_duties',
        'final_duties',
        'after_vacation_duties',
        'get_username',
    ]

    search_fields = ['faculty_id', 'full_name', 'email']
    readonly_fields = ['allocated_duties', 'relaxed_duties', 'final_duties', 'after_vacation_duties']

    def get_username(self, obj):
        return obj.user.username
    get_username.short_description = 'Username'

    def role_badge(self, obj):
        colors = {
            'deputy_superintendent': '#e65100',
            'adhoc': '#1565c0',
            'permanent': '#2e7d32',
            'guest': '#7b1fa2',
        }
        color = colors.get(obj.role, '#757575')
        return format_html(
            '<span style="color:white;background:{};padding:2px 8px;'
            'border-radius:10px;font-size:11px;">{}</span>',
            color,
            obj.get_role_display(),
        )
    role_badge.short_description = 'Role'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'bulk-upload/',
                self.admin_site.admin_view(self.bulk_upload_view),
                name='teachers_teacher_bulk_upload'
            ),
            path(
                'download-excel/',
                self.admin_site.admin_view(self.download_teachers_excel),
                name='teachers_teacher_download_teachers_excel'
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['bulk_upload_url'] = reverse('admin:teachers_teacher_bulk_upload')
        extra_context['download_excel_url'] = reverse('admin:teachers_teacher_download_teachers_excel')
        return super().changelist_view(request, extra_context=extra_context)

    def bulk_upload_view(self, request):
        if request.method == 'POST':
            xlsx_file = request.FILES.get('xlsx_file')

            if not xlsx_file:
                self.message_user(request, "Please upload an Excel file (.xlsx).", level=messages.ERROR)
                return redirect(request.path)

            try:
                rows = parse_xlsx(xlsx_file)
            except Exception:
                self.message_user(request, "Could not read Excel file. Ensure it's a valid .xlsx.", level=messages.ERROR)
                return redirect(request.path)

            parsed = []

            for i, row in enumerate(rows, start=2):
                faculty_id = (row.get('faculty_id') or '').strip()
                full_name = (row.get('full_name') or '').strip()
                department_name = (row.get('department') or '').strip()
                email = (row.get('email') or '').strip()
                phone_number = (row.get('phone_number') or '').strip()
                role = (row.get('role') or 'permanent').strip().lower().replace(' ', '_')

                if not faculty_id:
                    self.message_user(request, f"Row {i}: faculty_id is required.", level=messages.ERROR)
                    return redirect(request.path)

                if not full_name:
                    self.message_user(request, f"Row {i}: full_name is required.", level=messages.ERROR)
                    return redirect(request.path)

                if not email:
                    self.message_user(request, f"Row {i}: email is required.", level=messages.ERROR)
                    return redirect(request.path)

                if role not in ['deputy_superintendent', 'adhoc', 'permanent', 'guest']:
                    self.message_user(
                        request,
                        f"Row {i}: Invalid role '{role}'. Must be one of: "
                        f"Deputy Superintendent, Adhoc, Permanent, Guest Teacher.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)

                department = None
                if department_name:
                    department, _ = Department.objects.get_or_create(name=department_name)

                # Generate default password from full_name
                parts = full_name.split()
                first_name = parts[0]
                last_name = parts[-1] if len(parts) > 1 else ''
                if last_name:
                    password = f"{first_name.lower()}.{last_name.lower()}@123"
                else:
                    password = f"{first_name.lower()}@123"

                parsed.append({
                    'faculty_id': faculty_id,
                    'full_name': full_name,
                    'email': email,
                    'phone_number': phone_number,
                    'role': role,
                    'department': department,
                    'password': password,
                })

            if not parsed:
                self.message_user(request, "Excel file is empty.", level=messages.ERROR)
                return redirect(request.path)

            created_count = 0
            updated_count = 0
            teacher_credentials = []

            for row in parsed:
                base_username = row['full_name'].replace(' ', '').lower() or "teacher"

                existing = Teacher.objects.filter(faculty_id=row['faculty_id']).first()

                if existing:
                    existing.full_name    = row['full_name']
                    existing.email        = row['email']
                    existing.phone_number = row['phone_number']
                    existing.role         = row['role']
                    existing.department = row['department']
                    existing.save()
                    updated_count += 1

                    teacher_credentials.append({
                        'name':       row['full_name'],
                        'faculty_id': row['faculty_id'],
                        'department': row['department'].name if row['department'] else '',
                        'username':   existing.user.username,
                        'password':   '(unchanged)',
                    })

                else:
                    username = base_username
                    counter = 1
                    while User.objects.filter(username=username).exists():
                        username = f"{base_username}{counter}"
                        counter += 1

                    user = User.objects.create_user(
                        username=username,
                        password=row['password'],
                    )

                    Teacher.objects.create(
                        user=user,
                        faculty_id=row['faculty_id'],
                        full_name=row['full_name'],
                        email=row['email'],
                        phone_number=row['phone_number'],
                        role=row['role'],
                        department=row['department'],
                    )

                    created_count += 1

                    teacher_credentials.append({
                        'name':       row['full_name'],
                        'faculty_id': row['faculty_id'],
                        'department': row['department'].name if row['department'] else '',
                        'username':   username,
                        'password':   row['password'],
                    })

            self.message_user(
                request,
                f"{created_count} teacher(s) created, {updated_count} updated.",
                level=messages.SUCCESS,
            )

            request.session['last_upload_credentials'] = teacher_credentials

            return TemplateResponse(request, 'admin/teacher_upload_success.html', {
                'title': 'Bulk Upload Complete',
                'opts':  Teacher._meta,
                'teachers': teacher_credentials,
            })

        return TemplateResponse(request, 'admin/teacher_bulk_upload.html', {
            'title': 'Bulk Upload Teachers',
            'opts': Teacher._meta,
        })

    def download_teachers_excel(self, request):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Teacher Credentials"
        ws.append(['Faculty ID', 'Full Name', 'Department', 'Username', 'Password'])

        credentials = request.session.pop('last_upload_credentials', None)

        if credentials:
            for t in credentials:
                ws.append([
                    t['faculty_id'],
                    t['name'],
                    t['department'],
                    t['username'],
                    t['password'],
                ])
        else:
            teachers = Teacher.objects.select_related('user', 'department').all()
            for t in teachers:
                ws.append([
                    t.faculty_id,
                    t.full_name,
                    t.department.name if t.department else '',
                    t.user.username,
                    '** password not available **',
                ])

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="teachers_credentials.xlsx"'
        return response

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            return TeacherCreationForm
        return TeacherChangeForm

    def save_model(self, request, obj, form, change):
        if not change:
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                password=form.cleaned_data['password1'],
            )
            obj.user = user
        obj.save()

    def delete_model(self, request, obj):
        user = obj.user
        obj.delete()
        user.delete()

    def delete_queryset(self, request, queryset):
        for teacher in queryset:
            user = teacher.user
            teacher.delete()
            user.delete()


# ─── Session Admin with Excel Upload ────────────────────────────────────────────

@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ['name', 'semester_type', 'status', 'booking_deadline_display', 'minimum_duty', 'room_capacity', 'guest_duties_per_teacher', 'created_at', 'setup_link']

    def booking_deadline_display(self, obj):
        from django.utils import timezone as tz
        deadline = obj.exam_dates.values_list('booking_deadline', flat=True).first()
        if deadline:
            local_dt = tz.localtime(deadline)
            return local_dt.strftime('%Y-%m-%d %H:%M')
        return '—'
    booking_deadline_display.short_description = 'Booking Deadline'
    readonly_fields = ['created_at']

    def setup_link(self, obj):
        return format_html(
            '<a class="button" href="{}">📂 Setup Session</a>',
            f'/admin/teachers/session/{obj.pk}/setup/'
        )
    setup_link.short_description = 'Setup'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:session_id>/setup/',
                self.admin_site.admin_view(self.setup_session_view),
                name='session_setup'
            ),
        ]
        return custom_urls + urls

    def has_delete_permission(self, request, obj=None):
        return True

    def setup_session_view(self, request, session_id):
        session = Session.objects.get(pk=session_id)

        if request.method == 'POST':
            excel_file = request.FILES.get('excel_file')
            practical_excel_file = request.FILES.get('practical_excel_file')
            booking_deadline_raw = request.POST.get('booking_deadline')
            buffer = request.POST.get('buffer', '0').strip()
            morning_start = request.POST.get('morning_start')
            morning_end = request.POST.get('morning_end')
            evening_start = request.POST.get('evening_start')
            evening_end = request.POST.get('evening_end')

            # ── Session-level fields ──
            semester_type = request.POST.get('semester_type', 'odd')
            vacation_start_date_str = request.POST.get('vacation_start_date', '').strip()
            minimum_duty_str = request.POST.get('minimum_duty', '1').strip()

            session.semester_type = semester_type

            if vacation_start_date_str:
                from datetime import datetime as dt_mod
                try:
                    session.vacation_start_date = dt_mod.strptime(
                        vacation_start_date_str, "%Y-%m-%d"
                    ).date()
                except ValueError:
                    self.message_user(
                        request,
                        "Invalid vacation start date format (use YYYY-MM-DD).",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)
            else:
                session.vacation_start_date = None

            try:
                session.minimum_duty = max(1, int(minimum_duty_str))
            except ValueError:
                session.minimum_duty = 1

            # ── Room Capacity ──
            room_capacity_str = request.POST.get('room_capacity', '50').strip()
            try:
                session.room_capacity = max(1, int(room_capacity_str))
            except ValueError:
                session.room_capacity = 50

            # ── Guest duties per teacher (n) — even semester only ──
            guest_duties_str = request.POST.get('guest_duties_per_teacher', '').strip()
            if semester_type == 'even' and guest_duties_str:
                try:
                    session.guest_duties_per_teacher = max(0, int(guest_duties_str))
                except ValueError:
                    session.guest_duties_per_teacher = None
            else:
                session.guest_duties_per_teacher = None

            session.save()

            # ── Parse times ──
            from datetime import datetime

            def parse_time(value):
                for fmt in ("%H:%M", "%H:%M:%S"):
                    try:
                        return datetime.strptime(value, fmt).time()
                    except Exception:
                        continue
                return None

            morning_start = parse_time(morning_start)
            morning_end = parse_time(morning_end)
            evening_start = parse_time(evening_start)
            evening_end = parse_time(evening_end)

            if not all([morning_start, morning_end, evening_start, evening_end]):
                self.message_user(request, "Invalid time format.", level=messages.ERROR)
                return redirect(request.path)

            booking_deadline_dt = parse_datetime(booking_deadline_raw)
            if not booking_deadline_dt:
                self.message_user(request, "Invalid booking deadline format.", level=messages.ERROR)
                return redirect(request.path)

            from django.utils import timezone
            if timezone.is_naive(booking_deadline_dt):
                booking_deadline_dt = timezone.make_aware(booking_deadline_dt)

            try:
                buffer = int(buffer)
            except Exception:
                buffer = 0

            if not excel_file:
                self.message_user(request, "Upload exam Excel file (.xlsx).", level=messages.ERROR)
                return redirect(request.path)

            try:
                rows = parse_xlsx(excel_file)
            except Exception:
                self.message_user(request, "Could not read exam Excel file. Ensure it's a valid .xlsx.", level=messages.ERROR)
                return redirect(request.path)

            from collections import defaultdict
            data = defaultdict(list)

            for i, row in enumerate(rows, start=2):
                exam_name        = row.get('exam_name', '')
                date_str         = row.get('exam_date', '')
                morning_students = row.get('morning_students', '0')
                evening_students = row.get('evening_students', '0')

                if not all([exam_name, date_str]):
                    self.message_user(request, f"Row {i}: exam_name and exam_date are required.", level=messages.ERROR)
                    return redirect(request.path)

                try:
                    morning_students = int(morning_students)
                    evening_students = int(evening_students)
                    total_students = morning_students + evening_students
                    date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    self.message_user(request, f"Row {i}: Invalid data.", level=messages.ERROR)
                    return redirect(request.path)

                if morning_students == 0 and evening_students == 0:
                    self.message_user(request, f"Row {i}: Both morning and evening students cannot be 0.", level=messages.ERROR)
                    return redirect(request.path)

                data[date].append({
                    'exam_name':        exam_name,
                    'total_students':   total_students,
                    'morning_students': morning_students,
                    'evening_students': evening_students,
                })

            if not data:
                self.message_user(
                    request,
                    "No exam dates found in Excel file. Check your data.",
                    level=messages.ERROR,
                )
                return redirect(request.path)

            created_dates = 0
            created_slots = 0

            from .scheduler import schedule_auto_assignment

            for date, exams in data.items():
                total_students         = sum(e['total_students']   for e in exams)
                total_morning_students = sum(e['morning_students'] for e in exams)
                total_evening_students = sum(e['evening_students'] for e in exams)

                exam_date, created = ExamDate.objects.get_or_create(
                    session=session,
                    date=date,
                    defaults={
                        'total_students':   total_students,
                        'booking_deadline': booking_deadline_dt,
                        'morning_start':    morning_start,
                        'morning_end':      morning_end,
                        'evening_start':    evening_start,
                        'evening_end':      evening_end,
                    }
                )

                if created:
                    created_dates += 1
                else:
                    exam_date.total_students   = total_students
                    exam_date.booking_deadline = booking_deadline_dt
                    exam_date.morning_start    = morning_start
                    exam_date.morning_end      = morning_end
                    exam_date.evening_start    = evening_start
                    exam_date.evening_end      = evening_end
                    exam_date.save()

                # Schedule per-ExamDate auto-assignment
                schedule_auto_assignment(exam_date.booking_deadline, session.id, exam_date.id)

                exam_date.exams.all().delete()
                exam_date.slots.all().delete()

                # ── Create Exams ──
                for e in exams:
                    if e['morning_students'] > 0:
                        Exam.objects.create(
                            exam_date=exam_date,
                            exam_name=e['exam_name'],
                            students=e['morning_students'],
                            session='Morning'
                        )
                    if e['evening_students'] > 0:
                        Exam.objects.create(
                            exam_date=exam_date,
                            exam_name=e['exam_name'],
                            students=e['evening_students'],
                            session='Evening'
                        )

                # ── Create Slots ──
                if total_morning_students > 0:
                    morning_positions = math.ceil(total_morning_students / session.room_capacity) * 2 + buffer
                    Slot.objects.create(
                        exam_date=exam_date,
                        shift='Morning',
                        total_positions=morning_positions,
                        filled_positions=0
                    )
                    created_slots += morning_positions

                if total_evening_students > 0:
                    evening_positions = math.ceil(total_evening_students / session.room_capacity) * 2 + buffer
                    Slot.objects.create(
                        exam_date=exam_date,
                        shift='Evening',
                        total_positions=evening_positions,
                        filled_positions=0
                    )
                    created_slots += evening_positions

            # ── Process Practical Duty Excel & Calculate duties ──
            practical_skipped_rows = 0

            if practical_excel_file:
                PracticalExamDuty.objects.filter(session=session).delete()

                try:
                    prac_rows = parse_xlsx(practical_excel_file)
                except Exception:
                    self.message_user(
                        request,
                        "Could not read practical duty Excel file. Ensure it's a valid .xlsx.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)

                for i, row in enumerate(prac_rows, start=2):
                    teacher_name = row.get('teacher_name', '') or row.get('full_name', '')
                    faculty_id = row.get('faculty_id', '')
                    duty_hours_str = row.get('duty_hours', '0')

                    if not teacher_name or not faculty_id:
                        self.message_user(
                            request,
                            f"Practical Excel Row {i}: teacher_name and faculty_id are required.",
                            level=messages.ERROR,
                        )
                        return redirect(request.path)

                    try:
                        duty_hours = int(duty_hours_str)
                    except ValueError:
                        self.message_user(
                            request,
                            f"Practical Excel Row {i}: duty_hours must be a number.",
                            level=messages.ERROR,
                        )
                        return redirect(request.path)

                    PracticalExamDuty.objects.create(
                        session=session,
                        teacher_name=teacher_name,
                        faculty_id=faculty_id,
                        duty_hours=duty_hours,
                    )

            # ── Duty Calculation ──
            # Even semester: split into before-vacation and after-vacation
            # Odd semester: all slots treated as before-vacation (single pool)

            is_even = session.semester_type == 'even' and session.vacation_start_date
            vac_date = session.vacation_start_date

            # ── Compute X_before and X_after ──
            if is_even:
                X_before = Slot.objects.filter(
                    exam_date__session=session,
                    exam_date__date__lt=vac_date
                ).aggregate(total=db_models.Sum('total_positions'))['total'] or 0

                X_after = Slot.objects.filter(
                    exam_date__session=session,
                    exam_date__date__gte=vac_date
                ).aggregate(total=db_models.Sum('total_positions'))['total'] or 0
            else:
                X_before = Slot.objects.filter(
                    exam_date__session=session
                ).aggregate(total=db_models.Sum('total_positions'))['total'] or 0
                X_after = 0

            # ── Practical duty hours (Y) ──
            practical_duties = PracticalExamDuty.objects.filter(session=session)
            total_prac_hours = sum(p.duty_hours for p in practical_duties)
            Y = total_prac_hours // 3  # greatest integer function

            # Build faculty_id → practical hours lookup
            prac_hours_map = {}
            for pd in practical_duties:
                prac_hours_map[pd.faculty_id] = pd.duty_hours

            eligible_teachers = Teacher.objects.filter(
                role__in=['adhoc', 'permanent']
            )
            teacher_count = eligible_teachers.count()

            # ── Phase A: Before-Vacation (adhoc + permanent only) ──
            base_duty = 0
            remainder = 0

            if teacher_count > 0:
                total_pool = X_before + Y
                base_duty = total_pool // teacher_count
                remainder = total_pool % teacher_count

                eligible_list = list(eligible_teachers)
                random.shuffle(eligible_list)
                for i, teacher in enumerate(eligible_list):
                    teacher.allocated_duties = base_duty + (1 if i < remainder else 0)

                    hours = prac_hours_map.get(teacher.faculty_id, 0)
                    teacher.relaxed_duties = hours // 3  # greatest integer function

                    teacher.final_duties = max(
                        teacher.allocated_duties - teacher.relaxed_duties,
                        session.minimum_duty,
                    )
                    teacher.after_vacation_duties = 0  # set in Phase B below
                    teacher.save()

            # ── Phase B: After-Vacation (guest teachers first, then adhoc+permanent) ──
            Y_guest = 0
            n = 0

            if is_even and X_after > 0:
                n = session.guest_duties_per_teacher or 0
                guest_teachers = list(Teacher.objects.filter(role='guest'))
                guest_count = len(guest_teachers)
                Y_guest = n * guest_count

                # Set guest teacher duties
                for teacher in guest_teachers:
                    teacher.after_vacation_duties = n
                    teacher.allocated_duties = 0
                    teacher.relaxed_duties = 0
                    teacher.final_duties = 0
                    teacher.save()

                # Remaining after-vacation duties for adhoc+permanent
                if Y_guest < X_after and teacher_count > 0:
                    remaining = X_after - Y_guest
                    after_base = remaining // teacher_count
                    after_extra = remaining % teacher_count

                    for i, teacher in enumerate(eligible_list):
                        teacher.after_vacation_duties = after_base + (1 if i < after_extra else 0)
                        teacher.save()

            # ── Set deputies' duties to 0 ──
            deputies = Teacher.objects.filter(role='deputy_superintendent')
            for d in deputies:
                d.allocated_duties = 0
                d.relaxed_duties = 0
                d.final_duties = 0
                d.after_vacation_duties = 0
                d.save()

            # ── Set guest duties to 0 for odd semester ──
            if not is_even:
                for gt in Teacher.objects.filter(role='guest'):
                    gt.after_vacation_duties = 0
                    gt.allocated_duties = 0
                    gt.relaxed_duties = 0
                    gt.final_duties = 0
                    gt.save()

            # ── Activate session ──
            if not Session.objects.filter(status='active').exists():
                session.status = 'active'
                session.save()

            # ── Schedule auto assignment ──
            # Scheduled per-ExamDate inside the loop above

            # ── Success message ──
            if is_even:
                self.message_user(
                    request,
                    f"Setup complete! {created_dates} exam dates, {created_slots} total positions. "
                    f"Before-vacation: X={X_before}, Y={Y}, "
                    f"base={base_duty}/teacher (+1 for first {remainder} teachers), "
                    f"{teacher_count} eligible. | "
                    f"After-vacation: X_after={X_after}, n={n}, "
                    f"Y_guest={Y_guest}, remaining={X_after - Y_guest}.",
                    level=messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    f"Setup complete! {created_dates} exam dates, {created_slots} total positions. "
                    f"Duties: X={X_before}, Y={Y}, base={base_duty}/teacher "
                    f"(+1 for first {remainder} teachers), "
                    f"{teacher_count} eligible.",
                    level=messages.SUCCESS,
                )

            return redirect('/admin/teachers/session/')

        return render(request, 'admin/session_setup.html', {
            'session': session,
            'title': f'Setup Session: {session.name}',
            'opts': self.model._meta,
        })


# ─── Practical Exam Duty Admin ──────────────────────────────────────────────

@admin.register(PracticalExamDuty)
class PracticalExamDutyAdmin(admin.ModelAdmin):
    list_display = ['teacher_name', 'faculty_id', 'duty_hours', 'session']
    list_filter = ['session']
    search_fields = ['teacher_name', 'faculty_id']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'bulk-upload/',
                self.admin_site.admin_view(self.bulk_upload_view),
                name='teachers_practicalexamduty_bulk_upload',
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['bulk_upload_url'] = reverse(
            'admin:teachers_practicalexamduty_bulk_upload'
        )
        return super().changelist_view(request, extra_context=extra_context)

    def bulk_upload_view(self, request):
        if request.method == 'POST':
            xlsx_file = request.FILES.get('xlsx_file')

            if not xlsx_file:
                self.message_user(request, "Please upload an Excel file (.xlsx).", level=messages.ERROR)
                return redirect(request.path)

            try:
                rows = parse_xlsx(xlsx_file)
            except Exception:
                self.message_user(
                    request,
                    "Could not read Excel file. Ensure it's a valid .xlsx.",
                    level=messages.ERROR,
                )
                return redirect(request.path)

            created = 0
            skipped = 0

            for i, row in enumerate(rows, start=2):
                teacher_name = row.get('teacher_name', '') or row.get('full_name', '')
                faculty_id = row.get('faculty_id', '')
                duty_hours_str = row.get('duty_hours', '0')
                session_name = row.get('session', '')

                if not teacher_name or not faculty_id:
                    self.message_user(
                        request,
                        f"Row {i}: teacher_name and faculty_id are required.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)

                try:
                    duty_hours = int(duty_hours_str)
                except ValueError:
                    self.message_user(
                        request,
                        f"Row {i}: duty_hours must be a number.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)

                # Find the active session, or use the one specified
                if session_name:
                    ses = Session.objects.filter(name=session_name).first()
                else:
                    ses = Session.objects.filter(status='active').first()

                if not ses:
                    self.message_user(
                        request,
                        f"Row {i}: No active session found.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)

                _, was_created = PracticalExamDuty.objects.get_or_create(
                    session=ses,
                    faculty_id=faculty_id,
                    defaults={
                        'teacher_name': teacher_name,
                        'duty_hours': duty_hours,
                    }
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

            self.message_user(
                request,
                f"{created} practical duty record(s) added, {skipped} already existed.",
                level=messages.SUCCESS,
            )
            return redirect('admin:teachers_practicalexamduty_changelist')

        return TemplateResponse(request, 'admin/practical_duty_upload.html', {
            'title': 'Bulk Upload Practical Exam Duties',
            'opts': self.model._meta,
        })


# ─── ExamDate Admin ────────────────────────────────────────────────────────────

@admin.register(ExamDate)
class ExamDateAdmin(admin.ModelAdmin):
    list_display = ['date', 'session', 'total_students', 'rooms', 'slots_count', 'booking_deadline']
    readonly_fields = ['rooms', 'slots_count']
    list_filter = ['session']


# ─── Slot Admin ────────────────────────────────────────────────────────────────

@admin.register(Slot)
class SlotAdmin(admin.ModelAdmin):
    list_display = ['exam_date', 'shift', 'shift_students', 'total_positions', 'filled_positions', 'get_exams']
    readonly_fields = ['exam_date', 'shift', 'total_positions', 'filled_positions']
    list_filter = ['exam_date__session']

    def shift_students(self, obj):
        return obj.shift_students
    shift_students.short_description = 'Students (this shift)'

    def get_exams(self, obj):
        exams = obj.exam_date.exams.filter(session=obj.shift)
        names = [e.exam_name for e in exams]
        return mark_safe("<br>".join(names))
    get_exams.short_description = "Exams"

    def has_add_permission(self, request): return False

    def has_change_permission(self, request, obj=None):
        if obj: return False
        return True


# ─── Notification Admin ────────────────────────────────────────────────────────

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'activity', 'message', 'is_read', 'created_at']
    list_filter = ['activity', 'is_read']


# ─── Booking Admin ─────────────────────────────────────────────────────────────

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'slot', 'booked_at', 'auto_assigned']
    list_filter = ['auto_assigned']