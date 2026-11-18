import logging
from django.utils import timezone
from django.db.models import F
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django.db import transaction

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone=timezone.get_current_timezone())


def _build_tracking(all_bookings):
    """Build booking-count tracking dicts from a list of Booking objects."""
    teacher_booking_count = {}
    teacher_day_count = {}
    teacher_day_shifts = {}

    for booking in all_bookings:
        t_id = booking.teacher_id
        date = booking.slot.exam_date.date
        shift = booking.slot.shift

        teacher_booking_count[t_id] = teacher_booking_count.get(t_id, 0) + 1

        if t_id not in teacher_day_count:
            teacher_day_count[t_id] = {}
        teacher_day_count[t_id][date] = teacher_day_count[t_id].get(date, 0) + 1

        if t_id not in teacher_day_shifts:
            teacher_day_shifts[t_id] = {}
        if date not in teacher_day_shifts[t_id]:
            teacher_day_shifts[t_id][date] = set()
        teacher_day_shifts[t_id][date].add(shift)

    return teacher_booking_count, teacher_day_count, teacher_day_shifts


def _assign_slot_teachers(slot, teachers, duty_field, teacher_booking_count,
                          teacher_day_count, teacher_day_shifts):
    """Assign eligible teachers to an open slot, decrementing the given duty field."""
    from .models import Booking, Notification

    available = slot.total_positions - slot.filled_positions
    if available <= 0:
        return 0

    scored_teachers = []

    for teacher in teachers:
        # Re-read from DB to get fresh duty count
        teacher.refresh_from_db()
        remaining = getattr(teacher, duty_field)
        if remaining <= 0:
            continue

        t_id = teacher.teacher_id
        date = slot.exam_date.date
        shift = slot.shift

        # already booked same slot
        if Booking.objects.filter(teacher=teacher, slot=slot).exists():
            continue

        day_count = teacher_day_count.get(t_id, {}).get(date, 0)
        day_shifts = teacher_day_shifts.get(t_id, {}).get(date, set())

        # max 2 duties per day
        if day_count >= 2:
            continue

        # same shift twice
        if shift in day_shifts:
            continue

        score = 0

        # 1. Duties priority — more remaining = higher priority
        score += remaining * 10

        # 2. Workload balance — fewer total bookings so far = better
        bookings_done = teacher_booking_count.get(t_id, 0)
        score -= bookings_done * 5

        # 3. Discourage second duty on same day
        if day_count == 1:
            score -= 20

        scored_teachers.append((score, teacher))

    # sort by best score
    scored_teachers.sort(reverse=True, key=lambda x: x[0])

    assigned = 0

    for score, teacher in scored_teachers:
        if available <= 0:
            break

        # Re-read from DB one more time before assigning
        teacher.refresh_from_db()
        remaining = getattr(teacher, duty_field)
        if remaining <= 0:
            continue

        t_id = teacher.teacher_id
        date = slot.exam_date.date
        shift = slot.shift

        Booking.objects.create(
            teacher=teacher,
            slot=slot,
            auto_assigned=True
        )

        slot.filled_positions += 1
        slot.save()

        # Decrement the correct duty field
        setattr(teacher, duty_field, remaining - 1)
        teacher.save()

        # update tracking dicts (in-memory, for subsequent assignments)
        teacher_booking_count[t_id] = teacher_booking_count.get(t_id, 0) + 1

        if t_id not in teacher_day_count:
            teacher_day_count[t_id] = {}
        teacher_day_count[t_id][date] = teacher_day_count[t_id].get(date, 0) + 1

        if t_id not in teacher_day_shifts:
            teacher_day_shifts[t_id] = {}
        if date not in teacher_day_shifts[t_id]:
            teacher_day_shifts[t_id][date] = set()
        teacher_day_shifts[t_id][date].add(shift)

        Notification.objects.create(
            teacher=teacher,
            activity='auto_assigned',
            message=f"You were auto-assigned to {shift} shift on {date}."
        )

        available -= 1
        assigned += 1

    return assigned


def auto_assign_duties(session_id=None, exam_date_id=None):
    logger.info("Running auto_assign_duties...")

    from .models import ExamDate, Slot, Booking, Teacher, Notification, Session

    current_time = timezone.now()

    if session_id is not None:
        # Session-level lock held for entire call to prevent concurrent execution.
        # Each slot assignment uses its own transaction.atomic() savepoint so
        # partial failures don't roll back successful slot assignments.
        try:
            with transaction.atomic():
                active_session = Session.objects.select_for_update().get(pk=session_id)
                _run_assignment(active_session, exam_date_id, current_time)
        except Session.DoesNotExist:
            logger.error(f"Session {session_id} not found.")
            return
    else:
        active_session = Session.objects.filter(status='active').first()
        if not active_session:
            logger.info("No active session found.")
            return
        _run_assignment(active_session, exam_date_id, current_time)


def _run_assignment(active_session, exam_date_id, current_time):
    """Core assignment logic. Caller holds session lock if session_id was provided."""
    from .models import ExamDate, Slot, Booking, Teacher, Notification

    # Prevent empty-session completion
    exam_dates_qs = ExamDate.objects.filter(session=active_session)
    if not exam_dates_qs.exists():
        logger.info(f"Session {active_session.id} has no ExamDate records; skipping completion.")
        return

    # Determine vacation context
    is_even = (
        active_session.semester_type == 'even'
        and active_session.vacation_start_date is not None
    )
    vac_date = active_session.vacation_start_date

    # Filter exam dates for ASSIGNMENT: if exam_date_id specified, only process that one
    assignment_qs = exam_dates_qs.filter(booking_deadline__lt=current_time)
    if exam_date_id is not None:
        assignment_qs = assignment_qs.filter(pk=exam_date_id)

    expired_exams = list(assignment_qs)

    # Check if any future deadlines remain — session must not complete while
    # booking windows are still open. Check ALL exam dates, not just those
    # selected for assignment via exam_date_id.
    future_deadlines_exist = exam_dates_qs.filter(
        booking_deadline__gte=current_time
    ).exists()

    # PRELOAD BOOKINGS and build tracking once
    all_bookings = list(Booking.objects.filter(
        slot__exam_date__session=active_session
    ).select_related('slot__exam_date'))
    teacher_booking_count, teacher_day_count, teacher_day_shifts = _build_tracking(all_bookings)

    # Load teacher pools
    adhoc_perm_teachers = list(Teacher.objects.filter(
        role__in=['adhoc', 'permanent'],
    ))
    guest_teachers = list(Teacher.objects.filter(
        role='guest',
    ))

    any_failure = False

    for exam in expired_exams:
        open_slots = list(Slot.objects.filter(
            exam_date=exam,
            filled_positions__lt=F('total_positions')
        ))

        if not open_slots:
            continue

        for slot in open_slots:
            try:
                with transaction.atomic():
                    # Re-fetch slot from DB to get current filled_positions
                    slot = Slot.objects.select_for_update().get(pk=slot.pk)

                    is_after_vac = is_even and slot.exam_date.date >= vac_date

                    if is_after_vac:
                        # After-vacation: assign guest teachers first, then adhoc+permanent
                        _assign_slot_teachers(
                            slot, guest_teachers, 'after_vacation_duties',
                            teacher_booking_count, teacher_day_count, teacher_day_shifts
                        )
                        _assign_slot_teachers(
                            slot, adhoc_perm_teachers, 'after_vacation_duties',
                            teacher_booking_count, teacher_day_count, teacher_day_shifts
                        )
                    else:
                        # Before-vacation: only adhoc+permanent, using final_duties
                        _assign_slot_teachers(
                            slot, adhoc_perm_teachers, 'final_duties',
                            teacher_booking_count, teacher_day_count, teacher_day_shifts
                        )
            except Exception as e:
                logger.error(f"Failed to assign slot {slot.pk}: {e}")
                any_failure = True

    # Run recovery swap for any remaining vacancies
    _recover_vacancies(active_session, exam_date_id, current_time, is_even, vac_date)

    # Re-check remaining open slots after recovery
    all_expired_exams = list(exam_dates_qs.filter(booking_deadline__lt=current_time))
    remaining_open_slots = Slot.objects.filter(
        exam_date__in=all_expired_exams,
        filled_positions__lt=F('total_positions')
    ).exists()

    # Only complete when: no future deadlines, no remaining open slots,
    # and no assignment failures
    if not future_deadlines_exist and not remaining_open_slots and not any_failure:
        active_session.status = 'completed'
        active_session.save()
        logger.info("Auto assignment completed.")
    else:
        logger.info(
            "Auto assignment not complete — "
            f"future_deadlines={future_deadlines_exist}, "
            f"remaining_open_slots={remaining_open_slots}, "
            f"any_failure={any_failure}"
        )

def schedule_auto_assignment(deadline, session_id, exam_date_id=None):
    from django.utils.timezone import is_naive, make_aware
    if is_naive(deadline):
        deadline = make_aware(deadline)

    job_id = f'auto_assign_duties_{session_id}'
    if exam_date_id is not None:
        job_id = f'auto_assign_duties_{session_id}_{exam_date_id}'

    # Remove any existing job with this ID before adding, to guarantee
    # uniqueness. replace_existing=True alone is not reliable with
    # DjangoJobStore + SQLite.
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    scheduler.add_job(
        auto_assign_duties,
        trigger=DateTrigger(run_date=deadline),
        id=job_id,
        name=f'Auto assign session {session_id}' + (f' exam {exam_date_id}' if exam_date_id is not None else '') + f' at {deadline}',
        replace_existing=True,
        jobstore='default',
        kwargs={'session_id': session_id, 'exam_date_id': exam_date_id},
    )
    logger.info(f"Auto assignment scheduled for session {session_id}" + (f' exam {exam_date_id}' if exam_date_id is not None else '') + f' at {deadline}')


def start():
    # Add job store only if not already present
    if 'default' not in scheduler._jobstores:
        scheduler.add_jobstore(DjangoJobStore(), "default")
    if not scheduler.running:
        scheduler.start()

    # Clean up legacy auto_assign_duties job (no session/exam_date scoping)
    try:
        from django_apscheduler.models import DjangoJob
        legacy_jobs = DjangoJob.objects.filter(id='auto_assign_duties')
        if legacy_jobs.exists():
            legacy_jobs.delete()
            logger.info("Removed legacy auto_assign_duties job(s)")
    except Exception:
        pass

    # APScheduler's scheduler.start() already reloads all persisted jobs from
    # DjangoJobStore, including overdue jobs which it will execute according
    # to misfire_grace_time. No manual overdue scheduling is needed here.
    logger.info("Scheduler started.")


def find_sessions_needing_assignment():
    """
    Identify active sessions that have at least one ExamDate with an expired booking deadline.

    Returns a dict mapping session_id to list of expired ExamDate IDs.
    Only considers sessions with status='active' that have at least one ExamDate.
    Does not modify any data — read-only reconciliation.
    """
    from .models import Session, ExamDate
    from django.utils import timezone

    current_time = timezone.now()

    sessions_with_expired = Session.objects.filter(
        status='active',
        exam_dates__booking_deadline__lt=current_time
    ).distinct()

    result = {}
    for session in sessions_with_expired:
        expired_exam_dates = ExamDate.objects.filter(
            session=session,
            booking_deadline__lt=current_time
        ).values_list('id', flat=True)
        if expired_exam_dates:
            result[session.id] = list(expired_exam_dates)

    return result


def _can_teacher_take_slot(teacher, slot, is_even, vac_date, exclude_booking=None, credit=0):
    """
    Check if a teacher can be assigned to a slot, considering all constraints.

    exclude_booking: a Booking to exclude from day/shift counts (used when
                     the teacher is giving up that booking in a swap).
    credit: additional duty credit to add to the teacher's remaining quota
            (used when the teacher releases another slot in the same pool).
    """
    from .models import Booking

    is_after_vac = is_even and slot.exam_date.date >= vac_date
    duty_field = 'after_vacation_duties' if is_after_vac else 'final_duties'

    teacher.refresh_from_db()
    remaining = getattr(teacher, duty_field) + credit
    if remaining <= 0:
        return False

    if teacher.role == 'deputy_superintendent':
        return False

    if teacher.role == 'guest' and not is_after_vac:
        return False

    if Booking.objects.filter(teacher=teacher, slot=slot).exists():
        return False

    date = slot.exam_date.date
    day_qs = Booking.objects.filter(teacher=teacher, slot__exam_date__date=date)
    if exclude_booking is not None:
        day_qs = day_qs.exclude(id=exclude_booking.id)
    if day_qs.count() >= 2:
        return False

    shift_qs = Booking.objects.filter(
        teacher=teacher,
        slot__exam_date__date=date,
        slot__shift=slot.shift
    )
    if exclude_booking is not None:
        shift_qs = shift_qs.exclude(id=exclude_booking.id)
    if shift_qs.exists():
        return False

    return True


def _execute_swap(teacher_a, teacher_b, slot_y, slot_x, duty_field_a, duty_field_y,
                  is_even, vac_date):
    """
    Execute a swap: Teacher A takes Slot Y, Teacher B takes Slot X.

    Teacher A (with unused quota) takes Slot Y → consumes duty_field_y.
    Teacher B (displaced) gives up Slot Y (+1 duty_field_y) and takes Slot X
    (-1 duty_field_x). Net: Teacher B keeps same total duties; Slot X vacancy
    is filled; Slot Y remains filled (by Teacher A instead of Teacher B).

    All eligibility checks are re-run under row locks to prevent race conditions.
    """
    from .models import Booking, Notification, Teacher, Slot
    from django.db import transaction

    with transaction.atomic():
        teacher_a = Teacher.objects.select_for_update().get(pk=teacher_a.pk)
        teacher_b = Teacher.objects.select_for_update().get(pk=teacher_b.pk)
        slot_y = Slot.objects.select_for_update().get(pk=slot_y.pk)
        slot_x = Slot.objects.select_for_update().get(pk=slot_x.pk)

        # Lock and revalidate the original auto-assigned booking for Teacher B at Slot Y
        booking_b = Booking.objects.select_for_update().get(
            teacher=teacher_b, slot=slot_y
        )
        if not booking_b.auto_assigned:
            raise ValueError("Booking at Slot Y is not auto-assigned; refusing to move")

        # Re-verify eligibility under lock
        credit_b = 1 if duty_field_y == duty_field_a else 0
        if not _can_teacher_take_slot(teacher_a, slot_y, is_even, vac_date):
            raise ValueError("Teacher A cannot take Slot Y under lock")
        if not _can_teacher_take_slot(teacher_b, slot_x, is_even, vac_date,
                                      exclude_booking=booking_b, credit=credit_b):
            raise ValueError("Teacher B cannot take Slot X under lock")

        # Delete only the verified auto-assigned Booking at Slot Y
        booking_b.delete()

        # Create Teacher A's booking at Slot Y
        Booking.objects.create(
            teacher=teacher_a,
            slot=slot_y,
            auto_assigned=True
        )

        # Create Teacher B's booking at Slot X
        Booking.objects.create(
            teacher=teacher_b,
            slot=slot_x,
            auto_assigned=True
        )

        # Update slot counters: Slot X gains 1, Slot Y unchanged
        Slot.objects.filter(pk=slot_x.pk).update(filled_positions=F('filled_positions') + 1)

        # Update teacher duty counters
        Teacher.objects.filter(pk=teacher_a.pk).update(
            **{duty_field_y: F(duty_field_y) - 1}
        )
        if duty_field_y == duty_field_a:
            # Same pool: net change for Teacher B is 0
            Teacher.objects.filter(pk=teacher_b.pk).update(
                **{duty_field_y: F(duty_field_y)}
            )
        else:
            # Different pools: Teacher B gives up Y (+1), takes X (-1)
            Teacher.objects.filter(pk=teacher_b.pk).update(
                **{duty_field_y: F(duty_field_y) + 1}
            )
            Teacher.objects.filter(pk=teacher_b.pk).update(
                **{duty_field_a: F(duty_field_a) - 1}
            )

        Notification.objects.create(
            teacher=teacher_a,
            activity='auto_assigned',
            message=f"You were auto-assigned to {slot_y.shift} shift on {slot_y.exam_date.date}."
        )
        Notification.objects.create(
            teacher=teacher_b,
            activity='auto_assigned',
            message=f"You were auto-assigned to {slot_x.shift} shift on {slot_x.exam_date.date}."
        )


def _recover_vacancies(active_session, exam_date_id, current_time, is_even, vac_date):
    """
    Attempt to fill remaining vacancies via one-step swap recovery.

    For each open slot (filled_positions < total_positions), finds a teacher
    with remaining quota who cannot fill the slot directly, and an
    auto-assigned teacher whose slot they can take instead, such that the
    displaced teacher can fill the original vacancy.

    Preserves all existing constraints: max 2 duties/day, no duplicate shift
    on same date, before/after-vacation quota separation, role eligibility,
    no manual booking modification.

    Returns the number of successful swaps.
    """
    from .models import ExamDate, Slot, Booking, Teacher

    all_expired_exams = list(
        ExamDate.objects.filter(
            session=active_session,
            booking_deadline__lt=current_time
        )
    )

    if exam_date_id is not None:
        all_expired_exams = [e for e in all_expired_exams if e.id == exam_date_id]

    if not all_expired_exams:
        return 0

    open_slots = list(Slot.objects.filter(
        exam_date__in=all_expired_exams,
        filled_positions__lt=F('total_positions')
    ).select_related('exam_date').order_by('exam_date__date', 'shift'))

    if not open_slots:
        return 0

    recovered = 0

    for slot in open_slots:
        duty_field = 'after_vacation_duties' if (
            is_even and slot.exam_date.date >= vac_date
        ) else 'final_duties'

        # Find teachers with remaining duty quota who cannot fill this slot
        # directly. We do NOT exclude teachers already booked in this slot -
        # those are exactly the ones who need a swap (they can't take this
        # slot directly but might take another teacher's slot).
        eligible_teachers = list(Teacher.objects.filter(
            role__in=['adhoc', 'permanent', 'guest'] if is_even and slot.exam_date.date >= vac_date else ['adhoc', 'permanent'],
            **{f'{duty_field}__gt': 0}
        ))

        for teacher_a in eligible_teachers:
            teacher_a.refresh_from_db()
            if getattr(teacher_a, duty_field) <= 0:
                continue

            # Teacher A has remaining quota but cannot fill this slot directly
            # (either already booked, day-count limit, or same-shift conflict).
            # Look for a swap: find an auto-assigned booking by another teacher
            # that Teacher A can take, where that teacher can take this slot.
            candidate_bookings = list(Booking.objects.filter(
                auto_assigned=True,
                slot__exam_date__session=active_session,
                slot__exam_date__booking_deadline__lt=current_time
            ).exclude(teacher=teacher_a).select_related(
                'slot', 'slot__exam_date', 'teacher'
            ))

            for booking_b in candidate_bookings:
                teacher_b = booking_b.teacher
                slot_y = booking_b.slot

                if slot_y.id == slot.id:
                    continue

                teacher_b.refresh_from_db()

                is_after_vac_y = is_even and slot_y.exam_date.date >= vac_date
                duty_field_y = 'after_vacation_duties' if is_after_vac_y else 'final_duties'
                duty_field_x = duty_field

                # Teacher A must have quota for Slot Y's duty field
                teacher_a.refresh_from_db()
                if getattr(teacher_a, duty_field_y) <= 0:
                    continue

                # Teacher B must have quota for Slot X's duty field
                if duty_field_y == duty_field_x:
                    # Same pool: releasing Slot Y (+1) and taking Slot X (-1)
                    # = net 0, so any non-negative quota works
                    pass
                else:
                    # Different pools: Teacher B needs quota for Slot X
                    if getattr(teacher_b, duty_field_x) <= 0:
                        continue

                # Check if Teacher A can take Slot Y
                if not _can_teacher_take_slot(teacher_a, slot_y, is_even, vac_date):
                    continue

                # Check if Teacher B can take Slot X (without counting Slot Y)
                credit_b = 1 if duty_field_y == duty_field_x else 0
                if not _can_teacher_take_slot(teacher_b, slot, is_even, vac_date,
                                              exclude_booking=booking_b,
                                              credit=credit_b):
                    continue

                # Both assignments are valid - execute the swap
                _execute_swap(teacher_a, teacher_b, slot_y, slot,
                              duty_field_a=duty_field, duty_field_y=duty_field_y,
                              is_even=is_even, vac_date=vac_date)
                recovered += 1
                break

    return recovered
