from .models import NotificationParent, NotificationStaff, NotificationStudent
from .utils import user_roles


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
