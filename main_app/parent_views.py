import json
import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.core.files.storage import FileSystemStorage
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse

from .forms import (LeaveReportStudentForm, ParentEditForm,
                    ParentLinkRequestFormSet, ParentRegistrationForm)
from .models import (Attendance, Course, CustomUser,
                     LeaveReportStudent, NotificationParent, NotificationStaff,
                     Parent, ParentStudentLink, Staff, Student, Subject)
from .utils import (attendance_with_leave_json, log_action, parent_can_access_student,
                    rate_limited, send_notification_email)

DEFAULT_PROFILE_PIC = 'dist/img/default-150x150.png'

logger = logging.getLogger(__name__)


def parent_register(request):
    reg_form = ParentRegistrationForm(request.POST or None, request.FILES or None)
    link_formset = ParentLinkRequestFormSet(request.POST or None, prefix='child')
    context = {
        'reg_form': reg_form,
        'link_formset': link_formset,
        'courses': Course.objects.all(),
        'page_title': 'Register as a Parent',
    }
    if request.method == 'POST':
        if rate_limited(request, 'parent_register', max_attempts=5, window_seconds=3600):
            messages.error(request, "Too many registration attempts. Please try again later.")
            return render(request, "main_app/parent_register.html", context)
        if reg_form.is_valid() and link_formset.is_valid():
            first_name = reg_form.cleaned_data.get('first_name')
            last_name = reg_form.cleaned_data.get('last_name')
            address = reg_form.cleaned_data.get('address')
            email = reg_form.cleaned_data.get('email')
            gender = reg_form.cleaned_data.get('gender')
            password = reg_form.cleaned_data.get('password')
            contact_number = reg_form.cleaned_data.get('contact_number')
            passport = request.FILES.get('profile_pic')
            # static(), not the bare DEFAULT_PROFILE_PIC constant - that's
            # just the relative path, which resolves against whatever page
            # it's later rendered on (broken depending on URL depth) rather
            # than the actual /static/... URL a browser can load.
            passport_url = static(DEFAULT_PROFILE_PIC)
            if passport is not None:
                fs = FileSystemStorage()
                filename = fs.save(passport.name, passport)
                passport_url = fs.url(filename)
            try:
                user = CustomUser.objects.create_user(
                    email=email, password=password, user_type=4,
                    first_name=first_name, last_name=last_name, profile_pic=passport_url)
                user.gender = gender
                user.address = address
                user.parent.contact_number = contact_number
                user.save()
                for child_form in link_formset:
                    if not child_form.cleaned_data:
                        continue  # an unfilled extra row
                    ParentStudentLink.objects.create(
                        parent=user.parent,
                        student=child_form.cleaned_data['student'],
                        relationship=child_form.cleaned_data['relationship'],
                        date_of_birth=child_form.cleaned_data['date_of_birth'],
                    )
                log_action(request, 'created', 'Parent', user)
                messages.success(
                    request,
                    "Registration submitted! You can log in now - the link(s) to your "
                    "child(ren) will show as pending until the Madrasa Admin approves them.")
                return redirect(reverse('login_page'))
            except Exception:
                logger.exception('Unhandled error in parent_register')
                # Generic on purpose - this is the one form on the whole
                # site reachable with no account at all, so a raw
                # exception string (e.g. a DB constraint name) must never
                # reach the client here the way it's tolerated elsewhere
                # for already-authenticated admin/staff/student users.
                messages.error(request, "Could not complete registration. Please try again.")
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
    return render(request, "main_app/parent_register.html", context)


def get_students_for_registration(request):
    if rate_limited(request, 'parent_register_search', max_attempts=60, window_seconds=60):
        return JsonResponse({'error': 'Too many requests'}, status=429)
    course_id = request.POST.get('course')
    try:
        course = get_object_or_404(Course, id=course_id)
        # is_active=True: a withdrawn/deactivated student shouldn't be
        # discoverable by this public, unauthenticated search.
        students = Student.objects.filter(course=course, admin__is_active=True).select_related('admin')
        student_data = [
            {"id": student.id, "name": student.admin.last_name + " " + student.admin.first_name}
            for student in students
        ]
        return JsonResponse(json.dumps(student_data), content_type='application/json', safe=False)
    except Exception:
        logger.exception("Failed to fetch students for parent registration")
        return JsonResponse({'error': 'Could not fetch students.'}, status=400)


def parent_view_notification(request):
    parent = get_object_or_404(Parent, admin=request.user)
    notifications = list(NotificationParent.objects.filter(parent=parent))
    context = {
        'notifications': notifications,
        'page_title': "View Notifications"
    }
    NotificationParent.objects.filter(parent=parent, is_read=False).update(is_read=True)
    return render(request, "parent_template/parent_view_notification.html", context)


def parent_home(request):
    parent = get_object_or_404(Parent, admin=request.user)
    links = ParentStudentLink.objects.filter(parent=parent).select_related('student__admin', 'student__course')
    context = {
        'links': links,
        'page_title': 'Parent Homepage',
    }
    return render(request, 'parent_template/home_content.html', context)


def parent_link_kid(request):
    parent = get_object_or_404(Parent, admin=request.user)
    formset = ParentLinkRequestFormSet(request.POST or None, prefix='child')
    context = {
        'formset': formset,
        'courses': Course.objects.all(),
        'page_title': 'Link Another Kid',
    }
    if request.method == 'POST':
        if formset.is_valid():
            try:
                for child_form in formset:
                    if not child_form.cleaned_data:
                        continue
                    ParentStudentLink.objects.create(
                        parent=parent,
                        student=child_form.cleaned_data['student'],
                        relationship=child_form.cleaned_data['relationship'],
                        date_of_birth=child_form.cleaned_data['date_of_birth'],
                    )
                messages.success(request, "Request(s) submitted for Madrasa Admin approval")
                return redirect(reverse('parent_home'))
            except Exception:
                logger.exception('Unhandled error in parent_link_kid')
                messages.error(request, "Could not submit - you may have already requested one of these students.")
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
    return render(request, "parent_template/parent_link_kid.html", context)


def parent_view_profile(request):
    parent = get_object_or_404(Parent, admin=request.user)
    form = ParentEditForm(request.POST or None, request.FILES or None, instance=parent)
    context = {'form': form, 'page_title': 'View/Update Profile'}
    if request.method == 'POST':
        try:
            if form.is_valid():
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                password = form.cleaned_data.get('password') or None
                address = form.cleaned_data.get('address')
                gender = form.cleaned_data.get('gender')
                contact_number = form.cleaned_data.get('contact_number')
                passport = request.FILES.get('profile_pic') or None
                admin = parent.admin
                if password is not None:
                    admin.set_password(password)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    admin.profile_pic = passport_url
                admin.first_name = first_name
                admin.last_name = last_name
                admin.address = address
                admin.gender = gender
                admin.save()
                parent.contact_number = contact_number
                parent.save()
                if password is not None:
                    # Otherwise changing your own password logs you out of
                    # your current session immediately (stale auth hash).
                    update_session_auth_hash(request, admin)
                messages.success(request, "Profile Updated!")
                return redirect(reverse('parent_view_profile'))
            else:
                messages.error(request, "Please check the form - some required fields are missing or invalid.")
                return render(request, "parent_template/parent_view_profile.html", context)
        except Exception as e:
            logger.exception('Unhandled error in parent_view_profile')
            messages.error(request, "Error Occurred While Updating Profile " + str(e))
            return render(request, "parent_template/parent_view_profile.html", context)
    return render(request, "parent_template/parent_view_profile.html", context)


def _approved_links(parent):
    return ParentStudentLink.objects.filter(parent=parent, status=1).select_related('student__admin', 'student__course')


def parent_view_attendance(request):
    parent = get_object_or_404(Parent, admin=request.user)
    links = _approved_links(parent)
    if request.method != 'POST':
        student_id = request.GET.get('student')
        active_link = links.filter(student_id=student_id).first() if student_id else None
        if active_link is None:
            active_link = links.first()
        active_student = active_link.student if active_link else None
        subjects = (Subject.objects.filter(course=active_student.course)
                    if active_student and active_student.course_id else Subject.objects.none())
        context = {
            'links': links,
            'active_student': active_student,
            'subjects': subjects,
            'page_title': 'View Attendance',
        }
        return render(request, 'parent_template/parent_view_attendance.html', context)
    else:
        student_id = request.POST.get('student')
        subject_id = request.POST.get('subject')
        start = request.POST.get('start_date')
        end = request.POST.get('end_date')
        student = parent_can_access_student(parent, student_id)
        if student is None:
            return JsonResponse({'error': 'Not authorized for this student'}, status=404)
        try:
            # Scoped to the child's own class, same reasoning as
            # student_views.student_view_attendance.
            subject = get_object_or_404(Subject, id=subject_id, course=student.course)
            start_date = datetime.strptime(start, "%Y-%m-%d")
            end_date = datetime.strptime(end, "%Y-%m-%d")
            attendance = Attendance.objects.filter(date__range=(start_date, end_date), subject=subject)
            json_data = attendance_with_leave_json(student, attendance)
            return JsonResponse(json.dumps(json_data), safe=False)
        except Exception:
            logger.exception("Failed to fetch parent-viewed attendance")
            return JsonResponse({'error': 'Could not fetch attendance.'}, status=400)


def parent_apply_leave(request):
    parent = get_object_or_404(Parent, admin=request.user)
    links = _approved_links(parent)
    student_id = request.POST.get('student') or request.GET.get('student')
    active_link = links.filter(student_id=student_id).first() if student_id else None
    if active_link is None:
        active_link = links.first()
    active_student = active_link.student if active_link else None
    form = LeaveReportStudentForm(request.POST or None)
    context = {
        'form': form,
        'links': links,
        'active_student': active_student,
        'leave_history': LeaveReportStudent.objects.filter(student=active_student) if active_student else [],
        'page_title': 'Apply for leave',
    }
    if request.method == 'POST':
        target_student = parent_can_access_student(parent, student_id)
        if target_student is None:
            messages.error(request, "You are not authorized to apply leave for that student.")
            return render(request, "parent_template/parent_apply_leave.html", context)
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.student = target_student
                obj.applied_by_parent = parent
                obj.save()
                message = f"{target_student} applied for leave on {obj.date}: {obj.message} (submitted by parent)"
                # Same "every teacher of this class" fan-out student_apply_leave uses -
                # a class can have more than one teacher.
                teachers = Staff.objects.filter(subject__course=target_student.course).distinct()
                for teacher in teachers:
                    NotificationStaff.objects.create(staff=teacher, message=message)
                    send_notification_email(teacher.admin, message)
                messages.success(request, "Application for leave has been submitted for review")
                return redirect(reverse('parent_apply_leave') + f'?student={target_student.id}')
            except Exception:
                logger.exception('Unhandled error in parent_apply_leave')
                messages.error(request, "Could not submit")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "parent_template/parent_apply_leave.html", context)
