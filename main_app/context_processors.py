from .models import NotificationParent, NotificationStaff, NotificationStudent
from .utils import user_roles


def site_settings(request):
    from .models import SiteSettings

    settings_obj = SiteSettings.load()
    context = {'site_settings': settings_obj}

    # Login/register/privacy-notice pages are reachable while anonymous and
    # still need branding, so (unlike unread_notifications below) this is
    # not guarded on request.user.is_authenticated.
    active_role = request.session.get('active_role') if hasattr(request, 'session') else None
    if active_role == '1' and not settings_obj.onboarding_completed:
        from .models import Course, Session, Staff, Student

        steps = [
            ('Academic Session', Session.objects.exists(), 'add_session'),
            ('Courses', Course.objects.exists(), 'add_course'),
            ('Teachers', Staff.objects.exists(), 'add_staff'),
            ('Students', Student.objects.exists(), 'add_student'),
        ]
        next_step = next((s for s in steps if not s[1]), None)
        context['setup_progress'] = {
            'steps': steps,
            'next_step': next_step,
            'all_done': next_step is None,
        }
    return context


def unread_notifications(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}

    roles = user_roles(user)
    active_role = request.session.get('active_role', user.user_type)

    if active_role == '2':
        count = NotificationStaff.objects.filter(staff__admin=user, is_read=False).count()
    elif active_role == '3':
        count = NotificationStudent.objects.filter(student__admin=user, is_read=False).count()
    elif active_role == '4':
        count = NotificationParent.objects.filter(parent__admin=user, is_read=False).count()
    else:
        count = 0

    return {
        'unread_notification_count': count,
        'active_role': active_role,
        'available_roles': roles,
    }
