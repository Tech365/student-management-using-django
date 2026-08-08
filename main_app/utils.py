import csv
import io
import logging

from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.http import HttpResponse

PAGE_SIZE = 25

logger = logging.getLogger(__name__)


def paginate(request, queryset, per_page=PAGE_SIZE):
    """Return the requested page of `queryset` using the `page` query param."""
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get('page'))


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


def csv_response(filename, header, rows):
    """Build a downloadable CSV HttpResponse from a header list and an
    iterable of row tuples/lists."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
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


def leave_decision_message(leave_date, status):
    """Message text for a leave application's approve/reject decision."""
    verdict = "approved" if status == 1 else "rejected"
    return f"Your leave application for {leave_date} was {verdict}."


def teacher_course_ids(staff):
    """IDs of every course `staff` teaches at least one subject in.

    A teacher can teach subjects in more than one class, and a class can
    have more than one teacher - this is the shared definition of "this
    staff member is one of this class's teachers" used for student-leave
    visibility/approval, rather than relying on Staff.course (which only
    holds a single "home" class and can't represent that)."""
    from .models import Subject
    return set(Subject.objects.filter(staff=staff).values_list('course_id', flat=True))


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
