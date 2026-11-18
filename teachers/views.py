import random
import functools
from django.db import models as db_models
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import get_object_or_404, render, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.core.mail import send_mail
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView
from django.urls import reverse_lazy

from teachers.models import (
    Booking, Session, Slot, Teacher,
    ExamDate, Notification
)


# ─── Teacher-profile authorization ──────────────────────────────────────────────

def has_teacher_profile(user):
    """Return True if the user has a linked Teacher profile."""
    return hasattr(user, 'teacher')


def get_teacher_or_403(request):
    """
    Return (teacher, None) if the user has a Teacher profile,
    or (None, JsonResponse_403) if not.
    """
    if not has_teacher_profile(request.user):
        return None, JsonResponse({
            'error': 'teacher_profile_required',
            'message': 'No teacher profile linked to this account.'
        }, status=403)
    return request.user.teacher, None


def teacher_required(view_func):
    """
    Decorator requiring a linked Teacher profile.

    - Staff/superusers WITHOUT a Teacher profile are NEVER allowed
      into teacher-specific views; they are redirected to /admin/
      (HTML) or given HTTP 403 (JSON).
    - Staff/superusers WITH a Teacher profile are allowed normally.
    - Non-staff users without a Teacher profile get HTTP 403 (JSON)
      or redirect to login (HTML).
    """
    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not has_teacher_profile(request.user):
            # JSON/AJAX requests
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'error': 'teacher_profile_required',
                    'message': 'No teacher profile linked to this account.'
                }, status=403)
            # HTML views: administrators go to admin, others to login
            if request.user.is_superuser or request.user.is_staff:
                return redirect('/admin/')
            messages.error(request, "No teacher profile linked to this account.")
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return _wrapped


class RoleAwareLoginView(LoginView):
    """Login view that redirects based on user type after successful login."""
    template_name = 'login.html'

    def get_success_url(self):
        user = self.request.user
        if has_teacher_profile(user):
            return reverse_lazy('dashboard')
        if user.is_superuser or user.is_staff:
            return '/admin/'
        return super().get_success_url()


# ─── Exam Slots Count ─────────────────────────────────────────────────────────
# Used by slot_hint.js in the admin session setup page.

def exam_slots_count(request, exam_date_id):
    exam = get_object_or_404(ExamDate, id=exam_date_id)
    slots = Slot.objects.filter(exam_date=exam).count()
    return JsonResponse({'slots_count': slots})


# ─── Dashboard ────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    if not has_teacher_profile(request.user):
        if request.user.is_superuser or request.user.is_staff:
            return redirect('/admin/')
        messages.error(request, "No teacher profile linked to this account.")
        return redirect('login')

    teacher = request.user.teacher
    current_time = timezone.now()

    active_session = Session.objects.filter(status__in=['active', 'completed']).order_by('-created_at').first()

    slots_with_seats = []
    before_vacation_slots = []
    after_vacation_slots = []
    my_bookings = Booking.objects.none()
    notifications = Notification.objects.filter(teacher=teacher, is_read=False)

    if active_session:
        all_slots = Slot.objects.select_related('exam_date').filter(
            exam_date__session=active_session
        ).order_by('exam_date__date', 'shift')

        my_bookings = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=active_session
        ).select_related('slot__exam_date')

        booked_slot_ids = list(my_bookings.values_list('slot_id', flat=True))

        # Determine if this is an even semester with a vacation date
        is_even = (
            active_session.semester_type == 'even'
            and active_session.vacation_start_date is not None
        )
        vac_date = active_session.vacation_start_date

        for slot in all_slots:
            slot.seats_left = slot.total_positions - slot.filled_positions
            slot.already_booked = slot.id in booked_slot_ids
            slot.booking_closed = current_time > slot.exam_date.booking_deadline

            # Tag each slot as before/after vacation
            slot.is_after_vacation = is_even and slot.exam_date.date >= vac_date

            if not slot.booking_closed:
                time_left = slot.exam_date.booking_deadline - current_time
                slot.time_remaining_seconds = int(time_left.total_seconds())
            else:
                slot.time_remaining_seconds = 0

            # Guest teachers: only see after-vacation slots
            if teacher.role == 'guest' and not slot.is_after_vacation:
                continue

            slots_with_seats.append(slot)

            # Split into separate lists for even semester
            if slot.is_after_vacation:
                after_vacation_slots.append(slot)
            else:
                before_vacation_slots.append(slot)

        for booking in my_bookings:
            booking.slot.booking_closed = (
                current_time > booking.slot.exam_date.booking_deadline
            )

    # For even semester, pass split lists; for odd, everything in one list
    is_even_sem = active_session and active_session.semester_type == 'even' and active_session.vacation_start_date

    return render(request, 'dashboard.html', {
        'teacher': teacher,
        'active_session': active_session,
        'slots': slots_with_seats,
        'before_vacation_slots': before_vacation_slots,
        'after_vacation_slots': after_vacation_slots,
        'is_even_sem': is_even_sem,
        'my_bookings': my_bookings,
        'notifications': notifications,
    })


# ─── Notifications ────────────────────────────────────────────────────────────

@login_required
@teacher_required
def mark_notifications_read(request):
    teacher, err = get_teacher_or_403(request)
    if err:
        return err
    Notification.objects.filter(
        teacher=teacher,
        is_read=False
    ).update(is_read=True)
    return JsonResponse({'status': 'ok'})


# ─── Dashboard API ────────────────────────────────────────────────────────────

@login_required
@teacher_required
def dashboard_data(request):
    teacher, err = get_teacher_or_403(request)
    if err:
        return err
    current_time = timezone.now()

    active_session = Session.objects.filter(status__in=['active', 'completed']).order_by('-created_at').first()

    unread_notifications = list(
        Notification.objects.filter(
            teacher=teacher,
            is_read=False
        ).values('id', 'activity', 'message', 'created_at')
    )

    for n in unread_notifications:
        n['created_at'] = str(n['created_at'])

    # Determine vacation context
    is_even = False
    vac_date = None
    if active_session and active_session.semester_type == 'even' and active_session.vacation_start_date:
        is_even = True
        vac_date = active_session.vacation_start_date

    slots_data = []
    bookings_data = []

    if active_session:
        all_slots = Slot.objects.select_related('exam_date').filter(
            exam_date__session=active_session
        ).order_by('exam_date__date', 'shift')

        my_bookings = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=active_session
        ).select_related('slot__exam_date')

        booked_slot_ids = list(my_bookings.values_list('slot_id', flat=True))

        for slot in all_slots:
            seats_left = slot.total_positions - slot.filled_positions
            booking_closed = current_time > slot.exam_date.booking_deadline
            is_after_vac = is_even and slot.exam_date.date >= vac_date

            # Guest teachers: only see after-vacation slots
            if teacher.role == 'guest' and not is_after_vac:
                continue

            time_remaining_seconds = None
            if not booking_closed:
                time_left = slot.exam_date.booking_deadline - current_time
                time_remaining_seconds = int(time_left.total_seconds())

            slots_data.append({
                'id': slot.id,
                'date': str(slot.exam_date.date),
                'shift': slot.shift,
                'seats_left': seats_left,
                'already_booked': slot.id in booked_slot_ids,
                'booking_closed': booking_closed,
                'time_remaining_seconds': time_remaining_seconds,
                'is_after_vacation': is_after_vac,
            })

        for booking in my_bookings:
            booking_closed = current_time > booking.slot.exam_date.booking_deadline
            is_after_vac = is_even and booking.slot.exam_date.date >= vac_date
            bookings_data.append({
                'id': booking.id,
                'date': str(booking.slot.exam_date.date),
                'shift': booking.slot.shift,
                'booking_closed': booking_closed,
                'auto_assigned': booking.auto_assigned,
                'is_after_vacation': is_after_vac,
            })

    return JsonResponse({
        'final_duties': teacher.final_duties,
        'allocated_duties': teacher.allocated_duties,
        'relaxed_duties': teacher.relaxed_duties,
        'after_vacation_duties': teacher.after_vacation_duties,
        'role': teacher.get_role_display(),
        'is_even_semester': is_even,
        'slots': slots_data,
        'bookings': bookings_data,
        'notifications': unread_notifications,
    })


# ─── Book Slot ────────────────────────────────────────────────────────────────

@login_required
@teacher_required
def book_slot(request, slot_id):
    teacher, err = get_teacher_or_403(request)
    if err:
        return err

    with transaction.atomic():
        slot = Slot.objects.select_for_update().get(id=slot_id)

        # Determine active session and whether this slot is after vacation
        active_session = Session.objects.filter(status='active').first()
        is_after_vacation = False
        if active_session and active_session.semester_type == 'even' and active_session.vacation_start_date:
            is_after_vacation = slot.exam_date.date >= active_session.vacation_start_date

        if teacher.role == 'deputy_superintendent':
            messages.error(request, "Deputy Superintendents are not assigned slot duties.")
            return redirect('dashboard')

        # Guest teachers can only book after-vacation slots
        if teacher.role == 'guest' and not is_after_vacation:
            messages.error(request, "Guest teachers can only book after-vacation slots.")
            return redirect('dashboard')

        if timezone.now() > slot.exam_date.booking_deadline:
            messages.error(request, "Booking deadline has passed.")
            return redirect('dashboard')

        if slot.filled_positions >= slot.total_positions:
            messages.error(request, "Slot is full.")
            return redirect('dashboard')

        if Booking.objects.filter(teacher=teacher, slot=slot).exists():
            messages.error(request, "You already booked this slot.")
            return redirect('dashboard')

        same_day = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date=slot.exam_date
        ).count()

        if same_day >= 2:
            messages.error(request, "Max 2 duties per day allowed.")
            return redirect('dashboard')

        if Booking.objects.filter(
            teacher=teacher,
            slot__exam_date=slot.exam_date,
            slot__shift=slot.shift
        ).exists():
            messages.error(request, "You already have this shift.")
            return redirect('dashboard')

        # Check the correct duty counter based on before/after vacation
        if is_after_vacation:
            if teacher.after_vacation_duties <= 0:
                messages.error(request, "No after-vacation duties remaining.")
                return redirect('dashboard')
        else:
            if teacher.final_duties <= 0:
                messages.error(request, "No duties remaining.")
                return redirect('dashboard')

        Booking.objects.create(teacher=teacher, slot=slot)

        slot.filled_positions += 1
        slot.save()

        # Decrement the correct counter
        if is_after_vacation:
            teacher.after_vacation_duties -= 1
        else:
            teacher.final_duties -= 1
        teacher.save()

    Notification.objects.create(
        teacher=teacher,
        activity='booked',
        message=f"You booked {slot.shift} on {slot.exam_date.date}."
    )

    messages.success(request, "Slot booked successfully.")
    return redirect('dashboard')


# ─── Cancel Booking ───────────────────────────────────────────────────────────

@login_required
@teacher_required
def cancel_booking(request, booking_id):
    teacher, err = get_teacher_or_403(request)
    if err:
        return err

    with transaction.atomic():
        try:
            booking = Booking.objects.select_related('slot').get(
                id=booking_id,
                teacher=teacher
            )
        except Booking.DoesNotExist:
            messages.error(request, "Booking not found or not yours to cancel.")
            return redirect('dashboard')
        slot = Slot.objects.select_for_update().get(id=booking.slot.id)

        if timezone.now() > slot.exam_date.booking_deadline:
            messages.error(request, "Cancellation deadline passed.")
            return redirect('dashboard')

        # Determine if this booking was for an after-vacation slot
        active_session = Session.objects.filter(status='active').first()
        is_after_vacation = False
        if active_session and active_session.semester_type == 'even' and active_session.vacation_start_date:
            is_after_vacation = slot.exam_date.date >= active_session.vacation_start_date

        if slot.filled_positions > 0:
            slot.filled_positions -= 1
            slot.save()

        # Increment the correct duty counter
        if is_after_vacation:
            teacher.after_vacation_duties += 1
        else:
            teacher.final_duties += 1
        teacher.save()

        booking.delete()

    Notification.objects.create(
        teacher=teacher,
        activity='cancelled',
        message=f"You cancelled {slot.shift} on {slot.exam_date.date}."
    )

    messages.success(request, "Booking cancelled.")
    return redirect('dashboard')


# ─── Change Password ──────────────────────────────────────────────────────────

@login_required
def change_password(request):
    if request.method == 'POST':
        old = request.POST.get('old_password', '').strip()
        new = request.POST.get('new_password', '').strip()
        confirm = request.POST.get('confirm_password', '').strip()

        user = authenticate(username=request.user.username, password=old)
        if not user:
            messages.error(request, "Incorrect old password.")
            return render(request, 'change_password.html')

        if new != confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, 'change_password.html')

        try:
            validate_password(new)
        except Exception as e:
            messages.error(request, str(e))
            return render(request, 'change_password.html')

        request.user.set_password(new)
        request.user.save()

        messages.success(request, "Password changed. Please log in again.")
        return redirect('login')

    return render(request, 'change_password.html')


# ─── Forgot Password ──────────────────────────────────────────────────────────

def forgot_password(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()

        try:
            user = User.objects.get(username=username)
            teacher = user.teacher
        except User.DoesNotExist:
            messages.error(request, "No account found with that username.")
            return render(request, 'forgot_password.html')
        except Teacher.DoesNotExist:
            messages.error(request, "No teacher profile linked to this account.")
            return render(request, 'forgot_password.html')

        if not teacher.email:
            messages.error(request, "No email address on file. Contact admin.")
            return render(request, 'forgot_password.html')

        otp = str(random.randint(100000, 999999))
        teacher.otp = otp
        teacher.otp_expiry = timezone.now() + timedelta(minutes=5)
        teacher.save()

        try:
            send_mail(
                'Your OTP — Exam Duty System',
                f'Hi, {teacher.full_name}\n\nYour OTP for password reset is: {otp}\n\nIt expires in 5 minutes.',
                None,
                [teacher.email],
                fail_silently=False
            )
        except Exception:
            messages.error(request, "Failed to send OTP email. Please try again.")
            return render(request, 'forgot_password.html')

        request.session['reset_username'] = username
        return redirect('forgot_password_verify')

    return render(request, 'forgot_password.html')

def forgot_password_verify(request):
    username = request.session.get('reset_username')
    if not username:
        return redirect('forgot_password')

    try:
        user = User.objects.get(username=username)
        teacher = user.teacher
    except (User.DoesNotExist, Teacher.DoesNotExist):
        messages.error(request, "Session expired. Please start again.")
        return redirect('forgot_password')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        new = request.POST.get('new_password', '').strip()
        confirm = request.POST.get('confirm_password', '').strip()

        if not teacher.otp_expiry or timezone.now() > teacher.otp_expiry:
            messages.error(request, "OTP has expired. Please request a new one.")
            return redirect('forgot_password')

        if otp != teacher.otp:
            messages.error(request, "Invalid OTP.")
            return render(request, 'forgot_password_verify.html')

        if new != confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, 'forgot_password_verify.html')

        try:
            validate_password(new)
        except Exception as e:
            messages.error(request, str(e))
            return render(request, 'forgot_password_verify.html')

        user.set_password(new)
        user.save()

        # Clear OTP fields after successful reset
        teacher.otp = None
        teacher.otp_expiry = None
        teacher.save()

        del request.session['reset_username']

        messages.success(request, "Password reset successful. Please log in.")
        return redirect('login')

    return render(request, 'forgot_password_verify.html')

# ─── Download Report ─────────────────────────────────────────────────────────

@login_required
@teacher_required
def download_my_report(request):
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from django.http import HttpResponse

    teacher, err = get_teacher_or_403(request)
    if err:
        return err

    session = (
        Session.objects.filter(
            status='completed',
            exam_dates__slots__booking__teacher=teacher
        ).distinct().order_by('-created_at').first()
    )

    if not session:
        messages.error(request, "No completed session report available.")
        return redirect('dashboard')

    bookings = (
        Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=session
        ).select_related('slot__exam_date').order_by('slot__exam_date__date', 'slot__shift')
    )

    if not bookings.exists():
        messages.error(request, "No bookings found for your account.")
        return redirect('dashboard')

    # ── PDF Setup ──
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.8*cm, bottomMargin=1.8*cm,
    )

    styles = getSampleStyleSheet()
    PRIMARY   = colors.HexColor("#1a73e8")
    DARK      = colors.HexColor("#1a1a2e")
    LIGHT_BG  = colors.HexColor("#f0f4f8")
    ROW_ALT   = colors.HexColor("#e8f0fe")
    WHITE     = colors.white
    GREY      = colors.HexColor("#888888")
    GREEN     = colors.HexColor("#2e7d32")
    MORNING_C = colors.HexColor("#fff8e1")
    EVENING_C = colors.HexColor("#e8eaf6")

    story = []

    # ── Header ──
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        "Deen Dayal Upadhyay College",
        ParagraphStyle("inst", parent=styles["Normal"], fontSize=9, textColor=GREY, spaceAfter=2)
    ))
    story.append(Paragraph(
        f"Exam Duty Report — {teacher.full_name}",
        ParagraphStyle("title", parent=styles["Title"], fontSize=16, textColor=DARK, spaceAfter=4)
    ))
    story.append(Paragraph(
        f"Faculty ID: {teacher.faculty_id}   |   "
        f"Department: {teacher.department.name if teacher.department else '—'}   |   "
        f"Role: {teacher.get_role_display()}   |   "
        f"Session: {session.name}",
        ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=GREY, spaceAfter=6)
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY, spaceAfter=12))

    # ── Table ──
    cell_st  = ParagraphStyle("cell",  parent=styles["Normal"], fontSize=8.5, leading=11)
    hcell_st = ParagraphStyle("hcell", parent=styles["Normal"], fontSize=9, leading=11,
                               fontName="Helvetica-Bold", textColor=WHITE)

    def wrap(text):
        safe = str(text).replace("&", "&").replace("<", "<").replace(">", ">")
        return Paragraph(safe, cell_st)

    def wrap_h(text):
        return Paragraph(text, hcell_st)

    header_row = [wrap_h(h) for h in ["#", "Date", "Shift", "Shift Time", "Assignment"]]
    rows = [header_row]

    total_morning = total_evening = auto_count = 0

    for i, booking in enumerate(bookings, 1):
        slot      = booking.slot
        exam_date = slot.exam_date

        if slot.shift == "Morning":
            shift_time = f"{exam_date.morning_start.strftime('%H:%M')}–{exam_date.morning_end.strftime('%H:%M')}"
            total_morning += 1
            shift_bg = ParagraphStyle("sb", parent=cell_st, backColor=MORNING_C)
        else:
            shift_time = f"{exam_date.evening_start.strftime('%H:%M')}–{exam_date.evening_end.strftime('%H:%M')}"
            total_evening += 1
            shift_bg = ParagraphStyle("sb", parent=cell_st, backColor=EVENING_C)

        is_auto = booking.auto_assigned
        if is_auto:
            auto_count += 1

        asgn_st = ParagraphStyle("asgn", parent=cell_st,
                                  textColor=GREY if is_auto else GREEN,
                                  fontName="Helvetica-Bold")

        rows.append([
            wrap(str(i)),
            wrap(exam_date.date.strftime("%d %b %Y")),
            Paragraph(slot.shift, shift_bg),
            wrap(shift_time),
            Paragraph("Auto" if is_auto else "Self-booked", asgn_st),
        ])

    col_widths = [0.8*cm, 2.6*cm, 2.0*cm, 3.5*cm, 3.5*cm]
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR",      (0, 0), (-1, 0), WHITE),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING",  (0, 0), (-1, 0), 8),
        ("TOPPADDING",     (0, 0), (-1, 0), 8),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 8.5),
        ("TOPPADDING",     (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 1), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 7),
        ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d0d0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ROW_ALT]),
        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(tbl)

    doc.build(story)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="my_duty_report_{teacher.faculty_id}.pdf"'
    )
    return response

