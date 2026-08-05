from .models import NotificationStaff, NotificationStudent


def unread_notifications(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}

    if user.user_type == '2':
        count = NotificationStaff.objects.filter(staff__admin=user, is_read=False).count()
    elif user.user_type == '3':
        count = NotificationStudent.objects.filter(student__admin=user, is_read=False).count()
    else:
        count = 0

    return {'unread_notification_count': count}
