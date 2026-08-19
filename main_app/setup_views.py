import logging

from django.contrib import messages
from django.core.mail import get_connection, send_mail
from django.http import JsonResponse
from django.shortcuts import redirect, render, reverse

from .forms import SiteSettingsEmailForm, SiteSettingsForm
from .models import Course, Session, SiteSettings, Staff, Student
from .utils import log_action

logger = logging.getLogger(__name__)

# A separate module from hod_views.py so LoginCheckMiddleWare can allow-list
# these views by module name (see the active_role=='1' onboarding-gate
# check) without touching the existing role-based routing.


def _require_admin(request):
    return request.session.get('active_role') == '1'


def setup_school_profile(request):
    if not _require_admin(request):
        return redirect(reverse('login_page'))

    instance = SiteSettings.load()
    form = SiteSettingsForm(request.POST or None, request.FILES or None, instance=instance)
    context = {'form': form, 'page_title': 'School Profile', 'wizard_step': 1, 'wizard_total': 3}

    if request.method == 'POST':
        if form.is_valid():
            form.save()
            log_action(request, 'updated', 'Site Settings', instance)
            messages.success(request, "School profile saved.")
            # Unlike the existing add_* screens (which redirect back to
            # themselves), this is one linear onboarding sequence - move
            # forward to the next step.
            return redirect(reverse('setup_email_settings'))
        messages.error(request, "Couldn't save this. Please check the form and try again.")

    return render(request, "hod_template/setup_school_profile.html", context)


def setup_email_settings(request):
    if not _require_admin(request):
        return redirect(reverse('login_page'))

    instance = SiteSettings.load()
    form = SiteSettingsEmailForm(request.POST or None, instance=instance)
    context = {'form': form, 'page_title': 'Email Settings', 'wizard_step': 2, 'wizard_total': 3}

    if request.method == 'POST':
        # Email is skippable - an admin without SMTP details on hand yet
        # shouldn't be stuck here. Either path marks the step "seen" so
        # LoginCheckMiddleWare's force-redirect stops firing.
        if 'skip' in request.POST:
            request.session['setup_email_step_seen'] = True
            return redirect(reverse('setup_review'))
        if form.is_valid():
            form.save()
            log_action(request, 'updated', 'Site Settings', instance)
            request.session['setup_email_step_seen'] = True
            messages.success(request, "Email settings saved.")
            return redirect(reverse('setup_review'))
        messages.error(request, "Couldn't save this. Please check the form and try again.")

    return render(request, "hod_template/setup_email_settings.html", context)


def setup_send_test_email(request):
    if not _require_admin(request) or request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Not authorized.'}, status=403)

    host = request.POST.get('email_host', '').strip()
    port = request.POST.get('email_port', '').strip()
    username = request.POST.get('email_host_user', '').strip()
    password = request.POST.get('email_host_password', '')
    use_tls = request.POST.get('email_use_tls') == 'on'

    if not host or not port or not username:
        return JsonResponse({'success': False, 'message': 'Host, port, and username are required.'})

    school_name = SiteSettings.load().school_name or "your school"
    try:
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=host, port=int(port), username=username, password=password,
            use_tls=use_tls, fail_silently=False,
        )
        # Deliberately not send_notification_email() - that swallows
        # failures, and the whole point of this button is to surface the
        # specific SMTP error back to the admin.
        send_mail(
            subject=f"Test email from {school_name}",
            message="This is a test email from your school management system.",
            from_email=username,
            recipient_list=[username],
            connection=connection,
            fail_silently=False,
        )
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'success': True, 'message': 'Test email sent successfully.'})


def setup_review(request):
    if not _require_admin(request):
        return redirect(reverse('login_page'))

    if request.method == 'POST':
        instance = SiteSettings.load()
        instance.onboarding_completed = True
        instance.save()
        log_action(request, 'updated', 'Site Settings', instance)
        messages.success(request, "Setup complete!")
        return redirect(reverse('admin_home'))

    context = {
        'page_title': 'Review & Finish',
        'wizard_step': 3, 'wizard_total': 3,
        'session_count': Session.objects.count(),
        'course_count': Course.objects.count(),
        'staff_count': Staff.objects.count(),
        'student_count': Student.objects.count(),
    }
    return render(request, "hod_template/setup_review.html", context)
