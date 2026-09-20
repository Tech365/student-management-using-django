import csv
import io
import json
import logging
from datetime import date, timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.templatetags.static import static
from django.urls import reverse

PAGE_SIZE = 25

logger = logging.getLogger(__name__)


def paginate(request, queryset, per_page=PAGE_SIZE):
    """Return the requested page of `queryset` using the `page` query param."""
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get('page'))


def client_ip(request):
    """The real client address behind the nginx reverse proxy.

    nginx sets X-Real-IP from $remote_addr on every request (proxy_set_header
    always overwrites, so a client can't spoof it) - gunicorn itself only
    ever sees the proxy's own loopback address, which is useless for rate
    limiting. Falls back to REMOTE_ADDR for local/dev/test runs with no
    proxy in front."""
    return request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR', 'unknown')


def rate_limited(request, key, max_attempts, window_seconds):
    """True if this client has hit `key` more than `max_attempts` times in
    the last `window_seconds` - a simple fixed-window counter, good enough
    to blunt scripted brute-forcing/spam without adding a new dependency."""
    cache_key = f'ratelimit:{key}:{client_ip(request)}'
    attempts = cache.get(cache_key, 0)
    if attempts >= max_attempts:
        return True
    cache.set(cache_key, attempts + 1, window_seconds)
    return False


def read_csv_rows(uploaded_file):
    """Yield (row_number, dict) pairs from an uploaded CSV file.

    Row numbers start at 2 (row 1 is the header) to match what a user sees
    when they open the file in a spreadsheet. Column names and values are
    stripped of surrounding whitespace; utf-8-sig handles the BOM that
    Excel adds when it exports CSVs.
    """
    decoded = uploaded_file.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(decoded))
    for row_number, row in enumerate(reader, start=2):
        yield row_number, {
            (key.strip() if key else key): (value.strip() if value else value)
            for key, value in row.items()
        }


_FORMULA_LEAD_CHARS = ('=', '+', '-', '@', '\t', '\r')


def _neutralize_formula(value):
    """Prefix a cell with a leading `'` if it starts with a character a
    spreadsheet app would interpret as the start of a formula (CWE-1236 -
    CSV/DDE injection). Several exports here include free text a regular
    user can set (leave application messages, names, addresses), and this
    is the one choke point every CSV export already goes through."""
    text = str(value)
    if text.startswith(_FORMULA_LEAD_CHARS):
        return "'" + text
    return text


def csv_response(filename, header, rows):
    """Build a downloadable CSV HttpResponse from a header list and an
    iterable of row tuples/lists."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([_neutralize_formula(cell) for cell in header])
    for row in rows:
        writer.writerow([_neutralize_formula(cell) for cell in row])
    return response


def send_notification_email(user, message):
    """Email a notification to a CustomUser. Failures (bad address, SMTP
    outage) are logged and swallowed so a broken email never blocks the
    in-app notification or FCM push that already succeeded.

    Prefers this install's admin-configured SiteSettings SMTP fields when
    set; falls back to the server's static settings.EMAIL_*/DEFAULT_FROM_EMAIL
    (env vars) when they're blank - which reproduces today's behavior
    byte-for-byte for any install that hasn't been through the onboarding
    wizard's Email step yet."""
    if not user.email:
        return
    from .models import SiteSettings
    site_settings = SiteSettings.load()
    school_name = site_settings.school_name or "Madrasa Jamaliyah"
    connection = None
    from_email = None
    if site_settings.email_host:
        from django.core.mail import get_connection
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=site_settings.email_host,
            port=site_settings.email_port or 587,
            username=site_settings.email_host_user or None,
            password=site_settings.email_host_password or None,
            use_tls=site_settings.email_use_tls,
        )
        if site_settings.email_host_user:
            from_email = f"{school_name} <{site_settings.email_host_user}>"
    try:
        send_mail(
            subject=f"New notification - {school_name}",
            message=message,
            from_email=from_email,  # None falls back to DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            fail_silently=False,
            connection=connection,  # None falls back to settings.EMAIL_*
        )
    except Exception:
        logger.exception('Failed to send notification email to %s', user.email)


def send_push_notification(user, message, click_action_url_name=None):
    """Best-effort FCM push to `user`'s device, mirroring the same call
    shape hod_views.send_student_notification/send_staff_notification/
    send_parent_notification already use for admin broadcasts. Until now
    that was the *only* path that pushed - decisions (leave, parent-link
    approval) only ever emailed, even though a push tells someone the
    moment something about their kid was decided instead of whenever they
    next check their inbox. Silently does nothing without a registered
    token; failures are logged and swallowed, same as email."""
    if not user.fcm_token:
        return
    try:
        from .models import SiteSettings
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': SiteSettings.load().school_name or "Madrasa Jamaliyah",
                'body': message,
                'click_action': reverse(click_action_url_name) if click_action_url_name else '/',
                'icon': static('dist/img/AdminLTELogo.png')
            },
            'to': user.fcm_token
        }
        headers = {'Authorization': 'key=' + settings.FCM_SERVER_KEY, 'Content-Type': 'application/json'}
        requests.post(url, data=json.dumps(body), headers=headers, timeout=5)
    except Exception:
        logger.exception('Failed to send push notification to %s', user.email)


LEAVE_DECISION_VERDICTS = {1: "approved", -1: "rejected", 2: "cancelled"}


def leave_decision_message(leave_date, status):
    """Message text for a leave application's approve/reject/cancel decision."""
    verdict = LEAVE_DECISION_VERDICTS.get(status, "updated")
    return f"Your leave application for {leave_date} was {verdict}."


def parent_link_decision_message(student, status):
    """Message text for a parent-student link approve/reject decision."""
    verdict = "approved" if status == 1 else "rejected"
    return f"Your request to link with {student} was {verdict}."


def notify_student_leave_decision(leave, status):
    """Notify a decided-on student leave application: always the
    student's own account, and - if a parent submitted it on the
    student's behalf (parent_apply_leave) - that parent too, so they
    aren't left to find out only by asking their kid."""
    from .models import NotificationParent, NotificationStudent
    message = leave_decision_message(leave.date, status)
    NotificationStudent.objects.create(student=leave.student, message=message)
    send_notification_email(leave.student.admin, message)
    send_push_notification(leave.student.admin, message, 'student_view_notification')
    if leave.applied_by_parent_id:
        NotificationParent.objects.create(parent=leave.applied_by_parent, message=message)
        send_notification_email(leave.applied_by_parent.admin, message)
        send_push_notification(leave.applied_by_parent.admin, message, 'parent_view_notification')


def approved_leave_student_ids(students, attendance_date):
    """IDs of `students` with an approved leave for `attendance_date`.
    LeaveReportStudent.date is a free-text field fed by the same HTML5
    date input as attendance_date, so a plain string match is reliable."""
    from .models import LeaveReportStudent
    if not attendance_date:
        return set()
    return set(
        LeaveReportStudent.objects.filter(
            student__in=students, date=attendance_date, status=1
        ).values_list('student_id', flat=True)
    )


def resolve_attendance_subject(subject_id, staff=None):
    """Subject lookup for Take Attendance - scoped to `staff` (a teacher
    can only take attendance for their own subjects) or unscoped when
    staff is None (admin can take attendance for any class - e.g.
    filling in for an absent teacher)."""
    from django.shortcuts import get_object_or_404

    from .models import Subject
    if staff is not None:
        return get_object_or_404(Subject, id=subject_id, staff=staff)
    return get_object_or_404(Subject, id=subject_id)


def build_take_attendance_roster(subject, session, attendance_date):
    """The Take Attendance roster payload (students + already_taken/
    taken_by) - shared by the teacher-scoped screen
    (staff_views.get_students) and the admin any-class version
    (hod_views.admin_get_students)."""
    from django.db.models import Q

    from .models import Attendance, AttendanceReport, Student
    # admin__is_active=True: a deactivated student shouldn't still be
    # markable in the roster. Matches either the student's primary course
    # OR a secondary enrollment (e.g. a cross-cutting class like
    # "Miqaats" nobody has as their primary course) - distinct() guards
    # against a duplicate row from the secondary_courses join.
    students = Student.objects.filter(
        Q(course_id=subject.course_id) | Q(secondary_courses=subject.course_id),
        session=session, admin__is_active=True,
    ).distinct()
    on_leave_ids = approved_leave_student_ids(students, attendance_date)
    # A class can be co-taught (or, for admin, taken by a teacher and
    # then re-opened by admin) - if attendance already exists for this
    # exact subject/session/date, surface that instead of quietly
    # letting a second save re-mark from a blank slate.
    existing_attendance = Attendance.objects.filter(
        subject=subject, session=session, date=attendance_date).first()
    reports_by_student = {}
    if existing_attendance:
        reports_by_student = {
            r.student_id: r for r in AttendanceReport.objects.filter(attendance=existing_attendance)
        }
    student_data = []
    for student in students:
        report = reports_by_student.get(student.id)
        data = {
                "id": student.id,
                "name": student.admin.last_name + " " + student.admin.first_name,
                "on_leave": student.id in on_leave_ids,
                "status": report.status if report else None,
                # profile_pic is stored as a plain path string (see
                # hod_views.add_student), not a normal FileField upload,
                # so render it the same way every template does:
                # str(...) - not .url, which would double-prefix it.
                "profile_pic": str(student.admin.profile_pic),
                }
        student_data.append(data)
    return {
        "students": student_data,
        "already_taken": existing_attendance is not None,
        "taken_by": str(existing_attendance.taken_by) if existing_attendance and existing_attendance.taken_by else None,
    }


def save_take_attendance(subject, session, attendance_date, students, taken_by=None):
    """Create-or-update the Attendance + per-student AttendanceReport rows
    - shared by staff_views.save_attendance (taken_by=the teacher) and
    hod_views.admin_save_attendance (taken_by=None - Attendance.taken_by
    is a nullable FK to Staff, and every place that reads it already
    guards on truthiness, so an admin-saved record just has no name to
    show)."""
    from django.db.models import Q

    from .models import Attendance, AttendanceReport, Student
    attendance, created = Attendance.objects.get_or_create(
        session=session, subject=subject, date=attendance_date)
    # Whoever most recently saved it - lets a co-teacher (or admin) see
    # who took it last, not just who happened to create the row first.
    attendance.taken_by = taken_by
    attendance.save()

    submitted_ids = [student_dict.get('id') for student_dict in students]
    on_leave_ids = approved_leave_student_ids(
        Student.objects.filter(id__in=submitted_ids), attendance_date)

    for student_dict in students:
        # Scoped to the subject's own class (primary OR secondary
        # enrollment - see build_take_attendance_roster) - otherwise a
        # submitted student id from an unrelated class would get an
        # attendance record fabricated against this subject/session/date.
        from django.shortcuts import get_object_or_404
        student = get_object_or_404(
            Student.objects.filter(Q(course_id=subject.course_id) | Q(secondary_courses=subject.course_id)),
            id=student_dict.get('id'))
        if student.id in on_leave_ids:
            # Approved leave for this date - don't record attendance for
            # them at all, even if the client tried to send one.
            continue

        # get_or_create so re-taking attendance for the same student/date
        # (e.g. correcting a mistake) updates the existing report instead
        # of creating a duplicate.
        attendance_report, _ = AttendanceReport.objects.get_or_create(student=student, attendance=attendance)
        attendance_report.status = student_dict.get('status')
        attendance_report.save()


def teacher_course_ids(staff):
    """IDs of every course `staff` teaches at least one subject in.

    A teacher can teach subjects in more than one class, and a class can
    have more than one teacher - this is the shared definition of "this
    staff member is one of this class's teachers" used for student-leave
    visibility/approval, rather than relying on Staff.course (which only
    holds a single "home" class and can't represent that)."""
    from .models import Subject
    return set(Subject.objects.filter(staff=staff).values_list('course_id', flat=True))


def staff_class_names_map(staff_ids):
    """Map of Staff.id -> comma-joined class names, derived from Subject
    assignments (a teacher's classes aren't a single field on Staff -
    see teacher_course_ids). Staff with no subjects assigned yet are
    simply absent from the returned dict; look up with .get(id, '—')."""
    from .models import Subject
    staff_ids = set(staff_ids)
    by_staff = {}
    subjects = Subject.objects.filter(staff__id__in=staff_ids).distinct().prefetch_related('staff').select_related('course')
    for subject in subjects:
        # A subject can be co-taught, so attribute its class to every one
        # of its teachers that's actually in staff_ids - not just "the"
        # teacher, which no longer means anything for a shared subject.
        for staff_member in subject.staff.all():
            if staff_member.id in staff_ids:
                by_staff.setdefault(staff_member.id, set()).add(subject.course.name)
    return {staff_id: ', '.join(sorted(names)) for staff_id, names in by_staff.items()}


ROLE_MODELS = [('1', 'Admin', 'admin'), ('2', 'Staff', 'staff'),
               ('3', 'Student', 'student'), ('4', 'Parent', 'parent')]

ROLE_HOME_URL_NAMES = {'1': 'admin_home', '2': 'staff_home', '3': 'student_home', '4': 'parent_home'}


def user_roles(user):
    """[(code, label), ...] for every role `user` currently holds, derived
    from which profile row actually exists - NOT from user.user_type,
    which only means "the role picked at signup / default landing role,"
    not an exhaustive list once one account can hold more than one role."""
    return [(code, label) for code, label, attr in ROLE_MODELS if hasattr(user, attr)]


def missing_roles(user):
    """[(code, label), ...] for roles `user` does NOT currently hold - the
    complement of user_roles(), used to offer "grant this role too"
    shortcuts from the Manage/Edit screens."""
    held = {code for code, _ in user_roles(user)}
    return [(code, label) for code, label, attr in ROLE_MODELS if code not in held]


def grant_role(user, role_code, **extra):
    """Idempotently attach role `role_code` to an existing CustomUser -
    used by the admin-side "add an existing email to another role" flows.
    Never touches password/photo/name - those stay whatever they already
    are on the shared account. Returns the (possibly just-created)
    profile row."""
    from .models import Admin, Parent, Staff, Student
    model = {'1': Admin, '2': Staff, '3': Student, '4': Parent}[role_code]
    obj, _ = model.objects.get_or_create(admin=user)
    for field, value in extra.items():
        setattr(obj, field, value)
    if extra:
        obj.save()
    return obj


def role_home_url(role_code):
    """URL of the home/dashboard page for `role_code`, falling back to the
    login page for an unrecognized code rather than raising NoReverseMatch."""
    from django.urls import reverse
    return reverse(ROLE_HOME_URL_NAMES.get(role_code, 'login_page'))


def parent_can_access_student(parent, student_id):
    """The Student `parent` has an approved link to, or None.

    A parent can only view attendance/apply leave for a kid once an admin
    has approved that specific link - pending/rejected links (and links
    belonging to a different parent entirely) must not grant access."""
    from .models import ParentStudentLink
    link = ParentStudentLink.objects.filter(
        parent=parent, student_id=student_id, status=1).select_related('student').first()
    return link.student if link else None


def attendance_with_leave_json(student, attendance_qs):
    """Per-date attendance history for `student` across `attendance_qs` (an
    Attendance queryset already scoped to one subject/date range), with
    status "leave" filled in for dates the student has an approved leave
    for.

    A student on approved leave never gets an AttendanceReport row at all
    (see staff_views.save_attendance/update_attendance) - without this,
    those days would just silently vanish from the student's/parent's own
    attendance history instead of showing as Leave, the way the teacher's
    take/update-attendance screens already display it via on_leave_ids.
    """
    from .models import AttendanceReport, LeaveReportStudent
    attendance_by_id = {a.id: a for a in attendance_qs}
    reports = {
        r.attendance_id: r.status
        for r in AttendanceReport.objects.filter(attendance__in=attendance_by_id.values(), student=student)
    }
    leave_dates = set(
        LeaveReportStudent.objects.filter(
            student=student, status=1,
            date__in=[a.date.isoformat() for a in attendance_by_id.values()],
        ).values_list('date', flat=True)
    )
    json_data = []
    for attendance_id, attendance in attendance_by_id.items():
        date_str = attendance.date.isoformat()
        if attendance_id in reports:
            json_data.append({"date": date_str, "status": reports[attendance_id]})
        elif date_str in leave_dates:
            json_data.append({"date": date_str, "status": "leave"})
    json_data.sort(key=lambda d: d["date"])
    return json_data


def all_configured_school_weekdays():
    """Union of every Session's configured school_days, as a sorted list
    of ints (JS Date.getDay() convention). None if no session has
    school_days configured at all - used to grey out non-school-days on
    reports that aren't tied to one specific session (e.g. Attendance Not
    Taken), so a date is only disabled if EVERY configured session agrees
    it's not a school day."""
    from .models import Session
    configured = Session.objects.exclude(school_days='').values_list('school_days', flat=True)
    if not configured:
        return None
    days = set()
    for value in configured:
        days.update(int(d) for d in value.split(',') if d)
    return sorted(days)


def _js_weekday(date_obj):
    # Python's date.weekday() is Monday=0..Sunday=6; convert to JS
    # Date.getDay()'s Sunday=0..Saturday=6, matching Session.WEEKDAYS and
    # the client-side flatpickr disable logic in staff_take_attendance.html.
    return (date_obj.weekday() + 1) % 7


def is_school_day(session, date_obj):
    """True if `date_obj` falls on one of `session`'s configured
    school_days, or unconditionally True when school_days is blank
    (unrestricted, the default before this field existed)."""
    if not session.school_days:
        return True
    allowed = {int(d) for d in session.school_days.split(',') if d}
    return _js_weekday(date_obj) in allowed


def latest_school_day(session, today=None):
    """Most recent date on/before `today` that's a valid school day for
    `session` - the floor Take Attendance enforces, so a teacher can
    always catch up on the single most recently missed school day (or
    take it early, for a future one), but can't reach further back than
    that from this screen. Blank school_days (unrestricted) means every
    day counts, so this is just `today` itself."""
    today = today or date.today()
    candidate = today
    for _ in range(7):
        if is_school_day(session, candidate):
            return candidate
        candidate -= timedelta(days=1)
    return today  # unreachable - a non-empty school_days always matches within a week


def take_attendance_date_error(session, date_obj):
    """None if `date_obj` is acceptable for the Take Attendance screen;
    otherwise a user-facing reason it isn't. Server-side backstop for the
    same rule the client already enforces via flatpickr's minDate/disable
    (see staff_take_attendance.html) - nothing server-side validated this
    before, so a forged/replayed request could take attendance for any
    date at all."""
    if not is_school_day(session, date_obj):
        return "That date isn't a school day for the selected session."
    if date_obj < latest_school_day(session):
        return "That date is too far in the past for Take Attendance - use Update Attendance to correct an older record."
    return None


def apply_default_secondary_enrollment(user):
    """If this install has a default secondary class configured (see
    SiteSettings.default_secondary_course), enroll a freshly-created
    Student in it, or add a freshly-created Staff to teach all of its
    Subjects. Called right after a new Student/Staff role is created -
    both a brand-new account and an existing account just granted the
    role, since either way a new Student/Staff row is being created for
    the first time. A no-op when nothing's configured, so every existing
    install behaves exactly as before until an admin opts in."""
    from .models import SiteSettings, Subject
    course = SiteSettings.load().default_secondary_course
    if course is None:
        return
    if hasattr(user, 'student'):
        user.student.secondary_courses.add(course)
    elif hasattr(user, 'staff'):
        for subject in Subject.objects.filter(course=course):
            subject.staff.add(user.staff)


def attendance_not_taken_rows(selected_date, course_id='', staff=None):
    """Subjects with no Attendance row on `selected_date` - the shared
    computation behind both the admin-wide (hod_views.report_attendance_not_taken)
    and teacher-scoped (staff_views.staff_report_attendance_not_taken)
    "Attendance Not Taken" reports. `staff` optionally narrows this to
    just the subjects that teacher is assigned to - co-teaching aware,
    since it's the same Subject.staff M2M filter used everywhere else
    (see teacher_course_ids)."""
    from .models import Attendance, Subject
    subjects = (Subject.objects.select_related('course')
                .prefetch_related('staff__admin').order_by('course__name', 'name'))
    if staff is not None:
        subjects = subjects.filter(staff=staff)
    if course_id:
        subjects = subjects.filter(course_id=course_id)

    # Not scoped by Session - a subject with attendance taken in any
    # session on this date already has *something* recorded for it, and
    # session is a batch/cohort concept the person checking "did this
    # class get marked today" doesn't need to think about up front.
    taken_subject_ids = set(
        Attendance.objects.filter(date=selected_date, subject__in=subjects)
        .values_list('subject_id', flat=True)
    )
    return [
        {'course': subject.course.name, 'subject': subject.name,
         'teacher': ', '.join(str(t) for t in subject.staff.all())}
        for subject in subjects if subject.id not in taken_subject_ids
    ]


def session_course_ids_map():
    """Map of Session.id -> set of Course ids with at least one Student
    enrolled in that session (primary OR secondary - see
    Student.secondary_courses). A class isn't tied to one session (it
    spans years), but this lets a "Class" filter narrow the Session
    dropdown down to years that actually had students in that class,
    instead of listing every session the school has ever had.

    A purely-secondary class (e.g. a cross-cutting one like "Miqaats",
    which no student has as their *primary* course) still needs to show
    up here once at least one enrolled student's session is known -
    otherwise picking it as the Class anywhere in the app would show an
    empty Session dropdown and the whole flow would dead-end."""
    from .models import Student
    by_session = {}
    pairs = Student.objects.exclude(session=None).exclude(course=None).values_list('session_id', 'course_id')
    for session_id, course_id in pairs:
        by_session.setdefault(session_id, set()).add(course_id)
    secondary_pairs = Student.objects.exclude(session=None).values_list('session_id', 'secondary_courses__id')
    for session_id, course_id in secondary_pairs:
        if course_id is not None:
            by_session.setdefault(session_id, set()).add(course_id)
    return by_session


def log_action(request, action, target_model, target):
    """Record an admin mutation (create/update/delete/activate/deactivate)
    to the AuditLog. Imported lazily to avoid a circular import with
    models.py at module load time."""
    from .models import AuditLog
    AuditLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=action,
        target_model=target_model,
        target_repr=str(target)[:255],
    )
