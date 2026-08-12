import csv
import io
import json
import logging

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
    in-app notification or FCM push that already succeeded."""
    if not user.email:
        return
    try:
        send_mail(
            subject="New notification - Madrasa Jamaliyah",
            message=message,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            fail_silently=False,
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
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': "Madrasa Jamaliyah",
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


def leave_decision_message(leave_date, status):
    """Message text for a leave application's approve/reject decision."""
    verdict = "approved" if status == 1 else "rejected"
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
    by_staff = {}
    for subject in Subject.objects.filter(staff_id__in=staff_ids).select_related('course'):
        by_staff.setdefault(subject.staff_id, set()).add(subject.course.name)
    return {staff_id: ', '.join(sorted(names)) for staff_id, names in by_staff.items()}


def parent_can_access_student(parent, student_id):
    """The Student `parent` has an approved link to, or None.

    A parent can only view attendance/apply leave for a kid once an admin
    has approved that specific link - pending/rejected links (and links
    belonging to a different parent entirely) must not grant access."""
    from .models import ParentStudentLink
    link = ParentStudentLink.objects.filter(
        parent=parent, student_id=student_id, status=1).select_related('student').first()
    return link.student if link else None


def session_course_ids_map():
    """Map of Session.id -> set of Course ids with at least one Student
    enrolled in that session. A class isn't tied to one session (it
    spans years), but this lets a "Class" filter narrow the Session
    dropdown down to years that actually had students in that class,
    instead of listing every session the school has ever had."""
    from .models import Student
    by_session = {}
    pairs = Student.objects.exclude(session=None).exclude(course=None).values_list('session_id', 'course_id')
    for session_id, course_id in pairs:
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
