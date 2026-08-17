import json
import logging
from datetime import date, datetime, timedelta

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string

from .forms import (AdminForm, CourseForm, CSVUploadForm, GrantParentRoleForm,
                    ParentEditForm, SessionForm, StaffForm, StudentForm, SubjectForm)
from .models import (Admin, AttendanceReport, Attendance, AuditLog, Course,
                     CustomUser, FeedbackStaff, FeedbackStudent,
                     LeaveReportStaff, LeaveReportStudent, NotificationParent,
                     NotificationStaff, NotificationStudent, Parent,
                     ParentStudentLink, Session, Staff, Student,
                     StudentResult, Subject)
from .utils import (csv_response, grant_role, leave_decision_message, log_action,
                    notify_student_leave_decision, paginate,
                    parent_link_decision_message, read_csv_rows,
                    send_notification_email, send_push_notification,
                    session_course_ids_map, staff_class_names_map, user_roles)

DEFAULT_PROFILE_PIC = 'dist/img/default-150x150.png'
LEAVE_STATUS_LABELS = {0: 'Pending', 1: 'Approved', -1: 'Rejected'}
CHRONIC_ABSENCE_THRESHOLD = 75  # attendance % below this gets flagged in reports

logger = logging.getLogger(__name__)


def admin_home(request):
    total_staff = Staff.objects.all().count()
    total_students = Student.objects.all().count()
    subjects = Subject.objects.all()
    total_subject = subjects.count()
    total_course = Course.objects.all().count()
    total_pending_leave = (
        LeaveReportStudent.objects.filter(status=0).count()
        + LeaveReportStaff.objects.filter(status=0).count()
    )
    total_pending_parent_links = ParentStudentLink.objects.filter(status=0).count()
    attendance_counts = dict(
        Attendance.objects.filter(subject__in=subjects)
        .values_list('subject').annotate(count=Count('id'))
    )
    subject_list = [subject.name[:7] for subject in subjects]
    attendance_list = [attendance_counts.get(subject.id, 0) for subject in subjects]
    context = {
        'page_title': "Administrative Dashboard",
        'total_students': total_students,
        'total_staff': total_staff,
        'total_course': total_course,
        'total_subject': total_subject,
        'total_pending_leave': total_pending_leave,
        'total_pending_parent_links': total_pending_parent_links,
        'subject_list': subject_list,
        'attendance_list': attendance_list

    }
    return render(request, 'hod_template/home_content.html', context)


def global_search(request):
    """Search by name/email across every role at once - the fastest path
    to "someone called about their kid, pull up their record" without
    knowing which class/role to look under first."""
    query = request.GET.get('q', '').strip()
    results = []
    if query:
        matches = CustomUser.objects.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query) | Q(email__icontains=query)
        ).select_related('admin', 'staff', 'student', 'parent').order_by('last_name', 'first_name')[:50]
        role_edit_url = {'1': 'edit_admin', '2': 'edit_staff', '3': 'edit_student', '4': 'edit_parent'}
        role_object_attr = {'1': 'admin', '2': 'staff', '3': 'student', '4': 'parent'}
        for user in matches:
            # A match can hold more than one role now - list every one
            # they have, each with its own edit link, rather than only
            # the primary user_type (which would silently hide the rest).
            roles = []
            for code, label in user_roles(user):
                role_object = getattr(user, role_object_attr[code])
                roles.append({
                    'label': label,
                    'edit_url': reverse(role_edit_url[code], args=[role_object.id]),
                })
            if not roles:
                continue
            results.append({'user': user, 'roles': roles})
    context = {
        'query': query,
        'results': results,
        'page_title': 'Search',
    }
    return render(request, 'hod_template/global_search.html', context)


def _existing_user_for_role_attach(request):
    """The CustomUser already registered under the submitted email, or None
    if this is a POST to create a brand-new account. Also relaxes the
    password/photo fields to optional on `form` when one is found - those
    stay whatever they already are on the shared account, so an admin
    attaching a second role shouldn't be forced to re-enter them."""
    if request.method != 'POST':
        return None
    email = (request.POST.get('email') or '').strip().lower()
    return CustomUser.objects.filter(email=email).first() if email else None


def add_admin(request):
    form = AdminForm(request.POST or None, request.FILES or None)
    existing_user = _existing_user_for_role_attach(request)
    if existing_user is not None:
        form.fields['password'].required = False
        form.fields['profile_pic'].required = False
    context = {'form': form, 'page_title': 'Add Admin'}
    if request.method == 'POST':
        if form.is_valid():
            if existing_user is not None:
                if hasattr(existing_user, 'admin'):
                    messages.error(request, f"{existing_user} is already an Admin.")
                else:
                    grant_role(existing_user, '1')
                    log_action(request, 'granted', 'Admin', existing_user)
                    messages.success(request, f"Admin role added to {existing_user}'s existing account.")
                    return redirect(reverse('add_admin'))
            else:
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                address = form.cleaned_data.get('address')
                email = form.cleaned_data.get('email')
                gender = form.cleaned_data.get('gender')
                password = form.cleaned_data.get('password')
                passport = request.FILES.get('profile_pic')
                fs = FileSystemStorage()
                filename = fs.save(passport.name, passport)
                passport_url = fs.url(filename)
                try:
                    # create_user (not create_superuser): this grants the app-level
                    # HOD/Admin role (user_type=1) but not is_staff/is_superuser,
                    # so the new account can't log into Django's own /admin/ site.
                    user = CustomUser.objects.create_user(
                        email=email, password=password, user_type=1, first_name=first_name, last_name=last_name, profile_pic=passport_url)
                    user.gender = gender
                    user.address = address
                    user.save()
                    log_action(request, 'created', 'Admin', user)
                    messages.success(request, "Successfully Added")
                    return redirect(reverse('add_admin'))

                except Exception as e:
                    logger.exception('Unhandled error in add_admin')
                    messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")

    return render(request, 'hod_template/add_admin_template.html', context)


def add_staff(request):
    form = StaffForm(request.POST or None, request.FILES or None)
    existing_user = _existing_user_for_role_attach(request)
    if existing_user is not None:
        form.fields['password'].required = False
        form.fields['profile_pic'].required = False
    context = {'form': form, 'page_title': 'Add Staff'}
    if request.method == 'POST':
        if form.is_valid():
            if existing_user is not None:
                if hasattr(existing_user, 'staff'):
                    messages.error(request, f"{existing_user} is already a Staff member.")
                else:
                    grant_role(existing_user, '2')
                    log_action(request, 'granted', 'Staff', existing_user)
                    messages.success(request, f"Staff role added to {existing_user}'s existing account.")
                    return redirect(reverse('add_staff'))
            else:
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                address = form.cleaned_data.get('address')
                email = form.cleaned_data.get('email')
                gender = form.cleaned_data.get('gender')
                password = form.cleaned_data.get('password')
                passport = request.FILES.get('profile_pic')
                fs = FileSystemStorage()
                filename = fs.save(passport.name, passport)
                passport_url = fs.url(filename)
                try:
                    user = CustomUser.objects.create_user(
                        email=email, password=password, user_type=2, first_name=first_name, last_name=last_name, profile_pic=passport_url)
                    user.gender = gender
                    user.address = address
                    user.save()
                    log_action(request, 'created', 'Staff', user)
                    messages.success(request, "Successfully Added")
                    return redirect(reverse('add_staff'))

                except Exception as e:
                    logger.exception('Unhandled error in add_staff')
                    messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")

    return render(request, 'hod_template/add_staff_template.html', context)


def add_student(request):
    student_form = StudentForm(request.POST or None, request.FILES or None)
    existing_user = _existing_user_for_role_attach(request)
    if existing_user is not None:
        student_form.fields['password'].required = False
        student_form.fields['profile_pic'].required = False
    context = {'form': student_form, 'page_title': 'Add Student'}
    if request.method == 'POST':
        if student_form.is_valid():
            course = student_form.cleaned_data.get('course')
            session = student_form.cleaned_data.get('session')
            if existing_user is not None:
                if hasattr(existing_user, 'student'):
                    messages.error(request, f"{existing_user} is already a Student.")
                else:
                    grant_role(existing_user, '3', course=course, session=session)
                    log_action(request, 'granted', 'Student', existing_user)
                    messages.success(request, f"Student role added to {existing_user}'s existing account.")
                    return redirect(reverse('add_student'))
            else:
                first_name = student_form.cleaned_data.get('first_name')
                last_name = student_form.cleaned_data.get('last_name')
                address = student_form.cleaned_data.get('address')
                email = student_form.cleaned_data.get('email')
                gender = student_form.cleaned_data.get('gender')
                password = student_form.cleaned_data.get('password')
                passport = request.FILES.get('profile_pic')
                fs = FileSystemStorage()
                filename = fs.save(passport.name, passport)
                passport_url = fs.url(filename)
                try:
                    user = CustomUser.objects.create_user(
                        email=email, password=password, user_type=3, first_name=first_name, last_name=last_name, profile_pic=passport_url)
                    user.gender = gender
                    user.address = address
                    user.student.session = session
                    user.student.course = course
                    user.save()
                    log_action(request, 'created', 'Student', user)
                    messages.success(request, "Successfully Added")
                    return redirect(reverse('add_student'))
                except Exception as e:
                    logger.exception('Unhandled error in add_student')
                    messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, "Couldn't save this. Please check the form and try again.")
    return render(request, 'hod_template/add_student_template.html', context)


def grant_parent_role(request):
    """Attach the Parent role to an email that already has an account -
    deliberately attach-only (see GrantParentRoleForm's docstring): a
    brand-new Parent identity still only comes from public
    self-registration + the existing link-request/approval flow."""
    form = GrantParentRoleForm(request.POST or None)
    context = {'form': form, 'page_title': 'Grant Parent Role'}
    if request.method == 'POST':
        if form.is_valid():
            email = form.cleaned_data['email']
            contact_number = form.cleaned_data['contact_number']
            existing_user = CustomUser.objects.filter(email=email).first()
            if existing_user is None:
                messages.error(
                    request,
                    "No existing account with that email - new parents should "
                    "register at /parent/register/.")
            elif hasattr(existing_user, 'parent'):
                messages.error(request, f"{existing_user} is already a Parent.")
            else:
                grant_role(existing_user, '4', contact_number=contact_number)
                log_action(request, 'granted', 'Parent', existing_user)
                messages.success(request, f"Parent role added to {existing_user}'s existing account.")
                return redirect(reverse('grant_parent_role'))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
    return render(request, 'hod_template/grant_parent_role.html', context)


def add_course(request):
    form = CourseForm(request.POST or None)
    context = {
        'form': form,
        'page_title': 'Add Class'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            try:
                course = Course()
                course.name = name
                course.save()
                log_action(request, 'created', 'Class', course)
                messages.success(request, "Successfully Added")
                return redirect(reverse('add_course'))
            except:
                logger.exception('Unhandled error in add_course')
                messages.error(request, "Couldn't save this. Please check the form and try again.")
        else:
            messages.error(request, "Couldn't save this. Please check the form and try again.")
    return render(request, 'hod_template/add_course_template.html', context)


def add_subject(request):
    form = SubjectForm(request.POST or None)
    context = {
        'form': form,
        'page_title': 'Add Subject'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            course = form.cleaned_data.get('course')
            staff = form.cleaned_data.get('staff')
            try:
                subject = Subject()
                subject.name = name
                subject.staff = staff
                subject.course = course
                subject.save()
                log_action(request, 'created', 'Subject', subject)
                messages.success(request, "Successfully Added")
                return redirect(reverse('add_subject'))

            except Exception as e:
                logger.exception('Unhandled error in add_subject')
                messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, "Fill Form Properly")

    return render(request, 'hod_template/add_subject_template.html', context)


def bulk_upload_courses(request):
    form = CSVUploadForm()
    results = None
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            results = []
            for row_number, row in read_csv_rows(request.FILES['csv_file']):
                name = row.get('name') or ''
                if not name:
                    results.append({'row': row_number, 'status': 'error', 'message': 'Missing name'})
                    continue
                if Course.objects.filter(name__iexact=name).exists():
                    results.append({'row': row_number, 'status': 'skipped', 'message': f'"{name}" already exists'})
                    continue
                try:
                    Course.objects.create(name=name)
                    results.append({'row': row_number, 'status': 'success', 'message': f'Created "{name}"'})
                except Exception as e:
                    logger.exception('Unhandled error in bulk_upload_courses row %s', row_number)
                    results.append({'row': row_number, 'status': 'error', 'message': str(e)})
            messages.info(request, f"Processed {len(results)} row(s).")
    context = {'form': form, 'results': results, 'page_title': 'Bulk Upload Classes'}
    return render(request, 'hod_template/bulk_upload_courses.html', context)


def bulk_upload_subjects(request):
    form = CSVUploadForm()
    results = None
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            results = []
            for row_number, row in read_csv_rows(request.FILES['csv_file']):
                name = row.get('name') or ''
                staff_email = (row.get('staff_email') or '').lower()
                course_name = row.get('class') or ''
                missing = [f for f, v in [('name', name), ('staff_email', staff_email), ('class', course_name)] if not v]
                if missing:
                    results.append({'row': row_number, 'status': 'error', 'message': f"Missing {', '.join(missing)}"})
                    continue
                staff = Staff.objects.filter(admin__email__iexact=staff_email).first()
                if staff is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No staff with email "{staff_email}"'})
                    continue
                course = Course.objects.filter(name__iexact=course_name).first()
                if course is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No class named "{course_name}"'})
                    continue
                if Subject.objects.filter(name__iexact=name, course=course).exists():
                    results.append({'row': row_number, 'status': 'skipped', 'message': f'"{name}" already exists for {course_name}'})
                    continue
                try:
                    Subject.objects.create(name=name, staff=staff, course=course)
                    results.append({'row': row_number, 'status': 'success', 'message': f'Created "{name}"'})
                except Exception as e:
                    logger.exception('Unhandled error in bulk_upload_subjects row %s', row_number)
                    results.append({'row': row_number, 'status': 'error', 'message': str(e)})
            messages.info(request, f"Processed {len(results)} row(s).")
    context = {'form': form, 'results': results, 'page_title': 'Bulk Upload Subjects'}
    return render(request, 'hod_template/bulk_upload_subjects.html', context)


def bulk_upload_staff(request):
    form = CSVUploadForm()
    results = None
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            results = []
            for row_number, row in read_csv_rows(request.FILES['csv_file']):
                first_name = row.get('first_name') or ''
                last_name = row.get('last_name') or ''
                email = (row.get('email') or '').lower()
                gender = (row.get('gender') or '').upper()
                address = row.get('address') or ''

                missing = [f for f, v in [('first_name', first_name), ('last_name', last_name),
                                           ('email', email), ('gender', gender)] if not v]
                if missing:
                    results.append({'row': row_number, 'status': 'error', 'message': f"Missing {', '.join(missing)}"})
                    continue
                if gender not in ('M', 'F'):
                    results.append({'row': row_number, 'status': 'error', 'message': f'Gender must be M or F, got "{gender}"'})
                    continue
                if CustomUser.objects.filter(email=email).exists():
                    results.append({'row': row_number, 'status': 'skipped', 'message': f'"{email}" already registered'})
                    continue
                try:
                    password = get_random_string(10)
                    user = CustomUser.objects.create_user(
                        email=email, password=password, user_type=2,
                        first_name=first_name, last_name=last_name,
                        profile_pic=static(DEFAULT_PROFILE_PIC),
                    )
                    user.gender = gender
                    user.address = address
                    user.save()
                    results.append({'row': row_number, 'status': 'success',
                                     'message': f'Created — temporary password: {password}'})
                except Exception as e:
                    logger.exception('Unhandled error in bulk_upload_staff row %s', row_number)
                    results.append({'row': row_number, 'status': 'error', 'message': str(e)})
            messages.info(request, f"Processed {len(results)} row(s).")
    context = {'form': form, 'results': results, 'page_title': 'Bulk Upload Staff'}
    return render(request, 'hod_template/bulk_upload_staff.html', context)


def bulk_upload_students(request):
    form = CSVUploadForm()
    results = None
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            results = []
            for row_number, row in read_csv_rows(request.FILES['csv_file']):
                first_name = row.get('first_name') or ''
                last_name = row.get('last_name') or ''
                email = (row.get('email') or '').lower()
                gender = (row.get('gender') or '').upper()
                address = row.get('address') or ''
                course_name = row.get('class') or ''
                session_start = row.get('session_start_year') or ''
                session_end = row.get('session_end_year') or ''

                missing = [f for f, v in [('first_name', first_name), ('last_name', last_name),
                                           ('email', email), ('gender', gender), ('class', course_name),
                                           ('session_start_year', session_start),
                                           ('session_end_year', session_end)] if not v]
                if missing:
                    results.append({'row': row_number, 'status': 'error', 'message': f"Missing {', '.join(missing)}"})
                    continue
                if gender not in ('M', 'F'):
                    results.append({'row': row_number, 'status': 'error', 'message': f'Gender must be M or F, got "{gender}"'})
                    continue
                try:
                    datetime.strptime(session_start, "%Y-%m-%d")
                    datetime.strptime(session_end, "%Y-%m-%d")
                except ValueError:
                    results.append({'row': row_number, 'status': 'error',
                                     'message': 'Session dates must be YYYY-MM-DD'})
                    continue
                if CustomUser.objects.filter(email=email).exists():
                    results.append({'row': row_number, 'status': 'skipped', 'message': f'"{email}" already registered'})
                    continue
                course = Course.objects.filter(name__iexact=course_name).first()
                if course is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No class named "{course_name}"'})
                    continue
                session = Session.objects.filter(start_year=session_start, end_year=session_end).first()
                if session is None:
                    results.append({'row': row_number, 'status': 'error',
                                     'message': f'No session from {session_start} to {session_end}'})
                    continue
                try:
                    password = get_random_string(10)
                    user = CustomUser.objects.create_user(
                        email=email, password=password, user_type=3,
                        first_name=first_name, last_name=last_name,
                        profile_pic=static(DEFAULT_PROFILE_PIC),
                    )
                    user.gender = gender
                    user.address = address
                    user.save()
                    user.student.course = course
                    user.student.session = session
                    user.student.save()
                    results.append({'row': row_number, 'status': 'success',
                                     'message': f'Created — temporary password: {password}'})
                except Exception as e:
                    logger.exception('Unhandled error in bulk_upload_students row %s', row_number)
                    results.append({'row': row_number, 'status': 'error', 'message': str(e)})
            messages.info(request, f"Processed {len(results)} row(s).")
    context = {'form': form, 'results': results, 'page_title': 'Bulk Upload Students'}
    return render(request, 'hod_template/bulk_upload_students.html', context)


def manage_admin(request):
    # No paginate() here - DataTables (see main_app/templates/main_app/base.html)
    # owns search/sort/paging for this table client-side, over the full list.
    admins = CustomUser.objects.filter(admin__isnull=False).select_related('admin').order_by('id')
    context = {
        'admins': admins,
        'page_title': 'Manage Admins'
    }
    return render(request, "hod_template/manage_admin.html", context)


def manage_parent(request):
    parents = CustomUser.objects.filter(parent__isnull=False).select_related('parent').order_by('id')
    links_by_parent = {}
    for link in ParentStudentLink.objects.filter(parent__admin__in=parents).select_related('student__admin'):
        links_by_parent.setdefault(link.parent_id, []).append(link)
    for user in parents:
        user.links = links_by_parent.get(user.parent.id, [])
    context = {
        'parents': parents,
        'page_title': 'Manage Parents'
    }
    return render(request, "hod_template/manage_parent.html", context)


def manage_staff(request):
    allStaff = CustomUser.objects.filter(staff__isnull=False).select_related('staff').order_by('id')
    classes_by_staff = staff_class_names_map([user.staff.id for user in allStaff])
    for user in allStaff:
        user.class_names = classes_by_staff.get(user.staff.id, '—')
    context = {
        'allStaff': allStaff,
        'page_title': 'Manage Staff'
    }
    return render(request, "hod_template/manage_staff.html", context)


def manage_staff_csv(request):
    staff_qs = CustomUser.objects.filter(staff__isnull=False).select_related('staff').order_by('id')
    classes_by_staff = staff_class_names_map([user.staff.id for user in staff_qs])
    rows = [
        (user.last_name, user.first_name, user.email, user.gender,
         classes_by_staff.get(user.staff.id, ''), 'Active' if user.is_active else 'Inactive')
        for user in staff_qs
    ]
    return csv_response('staff.csv', ['Last Name', 'First Name', 'Email', 'Gender', 'Classes', 'Status'], rows)


def manage_student(request):
    students = CustomUser.objects.filter(student__isnull=False).select_related('student', 'student__course', 'student__session').order_by('id')
    context = {
        'students': students,
        'page_title': 'Manage Students'
    }
    return render(request, "hod_template/manage_student.html", context)


def manage_student_csv(request):
    students = CustomUser.objects.filter(student__isnull=False).select_related(
        'student', 'student__course', 'student__session').order_by('id')
    rows = [
        (user.last_name, user.first_name, user.email, user.gender,
         user.student.course.name if user.student.course_id else '',
         str(user.student.session) if user.student.session_id else '',
         'Active' if user.is_active else 'Inactive')
        for user in students
    ]
    return csv_response('students.csv',
                         ['Last Name', 'First Name', 'Email', 'Gender', 'Class', 'Session', 'Status'], rows)


def manage_course(request):
    courses = Course.objects.order_by('id')
    context = {
        'courses': courses,
        'page_title': 'Manage Classes'
    }
    return render(request, "hod_template/manage_course.html", context)


def manage_subject(request):
    subjects = Subject.objects.order_by('id')
    context = {
        'subjects': subjects,
        'page_title': 'Manage Subjects'
    }
    return render(request, "hod_template/manage_subject.html", context)


def edit_staff(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    form = StaffForm(request.POST or None, request.FILES or None, instance=staff)
    context = {
        'form': form,
        'staff_id': staff_id,
        'page_title': 'Edit Staff'
    }
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            email = form.cleaned_data.get('email')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=staff.admin.id)
                user.email = email
                if password is not None:
                    user.set_password(password)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                user.first_name = first_name
                user.last_name = last_name
                user.gender = gender
                user.address = address
                user.save()
                log_action(request, 'updated', 'Staff', user)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_staff', args=[staff_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_staff')
                messages.error(request, "Couldn't save your changes - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
        return render(request, "hod_template/edit_staff_template.html", context)
    else:
        return render(request, "hod_template/edit_staff_template.html", context)


def edit_student(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    form = StudentForm(request.POST or None, request.FILES or None, instance=student)
    context = {
        'form': form,
        'student_id': student_id,
        'page_title': 'Edit Student'
    }
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            email = form.cleaned_data.get('email')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            course = form.cleaned_data.get('course')
            session = form.cleaned_data.get('session')
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=student.admin.id)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                user.email = email
                if password is not None:
                    user.set_password(password)
                user.first_name = first_name
                user.last_name = last_name
                student.session = session
                user.gender = gender
                user.address = address
                student.course = course
                user.save()
                student.save()
                log_action(request, 'updated', 'Student', user)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_student', args=[student_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_student')
                messages.error(request, "Couldn't save your changes - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
        return render(request, "hod_template/edit_student_template.html", context)
    else:
        return render(request, "hod_template/edit_student_template.html", context)


def edit_admin(request, admin_id):
    admin = get_object_or_404(Admin, id=admin_id)
    form = AdminForm(request.POST or None, request.FILES or None, instance=admin)
    context = {
        'form': form,
        'admin_id': admin_id,
        'page_title': 'Edit Admin'
    }
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            email = form.cleaned_data.get('email')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=admin.admin.id)
                user.email = email
                if password is not None:
                    user.set_password(password)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                user.first_name = first_name
                user.last_name = last_name
                user.gender = gender
                user.address = address
                user.save()
                if password is not None and user.id == request.user.id:
                    # Keep the current session valid instead of forcing an
                    # immediate logout when a HOD changes their own password.
                    update_session_auth_hash(request, user)
                log_action(request, 'updated', 'Admin', user)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_admin', args=[admin_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_admin')
                messages.error(request, "Couldn't save your changes - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
        return render(request, "hod_template/edit_admin_template.html", context)
    else:
        return render(request, "hod_template/edit_admin_template.html", context)


def edit_parent(request, parent_id):
    parent = get_object_or_404(Parent, id=parent_id)
    form = ParentEditForm(request.POST or None, request.FILES or None, instance=parent)
    context = {
        'form': form,
        'parent_id': parent_id,
        'page_title': 'Edit Parent'
    }
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            email = form.cleaned_data.get('email')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            contact_number = form.cleaned_data.get('contact_number')
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=parent.admin.id)
                user.email = email
                if password is not None:
                    user.set_password(password)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                user.first_name = first_name
                user.last_name = last_name
                user.gender = gender
                user.address = address
                user.save()
                parent.contact_number = contact_number
                parent.save()
                if password is not None and user.id == request.user.id:
                    update_session_auth_hash(request, user)
                log_action(request, 'updated', 'Parent', user)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_parent', args=[parent_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_parent')
                messages.error(request, "Couldn't save your changes - " + str(e))
        else:
            messages.error(request, "Please check the form - some required fields are missing or invalid.")
        return render(request, "hod_template/edit_parent_template.html", context)
    else:
        return render(request, "hod_template/edit_parent_template.html", context)


def edit_course(request, course_id):
    instance = get_object_or_404(Course, id=course_id)
    form = CourseForm(request.POST or None, instance=instance)
    context = {
        'form': form,
        'course_id': course_id,
        'page_title': 'Edit Class'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            try:
                course = Course.objects.get(id=course_id)
                course.name = name
                course.save()
                log_action(request, 'updated', 'Class', course)
                messages.success(request, "Successfully Updated")
            except:
                logger.exception('Unhandled error in edit_course')
                messages.error(request, "Couldn't save your changes. Please check the form and try again.")
        else:
            messages.error(request, "Couldn't save your changes. Please check the form and try again.")

    return render(request, 'hod_template/edit_course_template.html', context)


def edit_subject(request, subject_id):
    instance = get_object_or_404(Subject, id=subject_id)
    form = SubjectForm(request.POST or None, instance=instance)
    context = {
        'form': form,
        'subject_id': subject_id,
        'page_title': 'Edit Subject'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            course = form.cleaned_data.get('course')
            staff = form.cleaned_data.get('staff')
            try:
                subject = Subject.objects.get(id=subject_id)
                subject.name = name
                subject.staff = staff
                subject.course = course
                subject.save()
                log_action(request, 'updated', 'Subject', subject)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_subject', args=[subject_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_subject')
                messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, "Fill Form Properly")
    return render(request, 'hod_template/edit_subject_template.html', context)


def add_session(request):
    form = SessionForm(request.POST or None)
    context = {'form': form, 'page_title': 'Add Session'}
    if request.method == 'POST':
        if form.is_valid():
            try:
                session = form.save()
                log_action(request, 'created', 'Session', session)
                messages.success(request, "Session Created")
                return redirect(reverse('add_session'))
            except Exception as e:
                logger.exception('Unhandled error in add_session')
                messages.error(request, "Couldn't save this - " + str(e))
        else:
            messages.error(request, 'Fill Form Properly ')
    return render(request, "hod_template/add_session_template.html", context)


def manage_session(request):
    sessions = Session.objects.order_by('id')
    context = {'sessions': sessions, 'page_title': 'Manage Sessions'}
    return render(request, "hod_template/manage_session.html", context)


def edit_session(request, session_id):
    instance = get_object_or_404(Session, id=session_id)
    form = SessionForm(request.POST or None, instance=instance)
    context = {'form': form, 'session_id': session_id,
               'page_title': 'Edit Session'}
    if request.method == 'POST':
        if form.is_valid():
            try:
                session = form.save()
                log_action(request, 'updated', 'Session', session)
                messages.success(request, "Session Updated")
                return redirect(reverse('edit_session', args=[session_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_session')
                messages.error(
                    request, "Session Could Not Be Updated " + str(e))
                return render(request, "hod_template/edit_session_template.html", context)
        else:
            messages.error(request, "Invalid Form Submitted ")
            return render(request, "hod_template/edit_session_template.html", context)

    else:
        return render(request, "hod_template/edit_session_template.html", context)


def check_email_availability(request):
    email = request.POST.get("email")
    try:
        user = CustomUser.objects.filter(email=email).exists()
        if user:
            return HttpResponse(True)
        return HttpResponse(False)
    except Exception as e:
        logger.exception('Unhandled error in check_email_availability')
        return HttpResponse(False)


def student_feedback_message(request):
    if request.method != 'POST':
        feedbacks = paginate(request, FeedbackStudent.objects.order_by('id'))
        context = {
            'feedbacks': feedbacks,
            'page_obj': feedbacks,
            'page_title': 'Student Feedback Messages'
        }
        return render(request, 'hod_template/student_feedback_template.html', context)
    else:
        feedback_id = request.POST.get('id')
        try:
            feedback = get_object_or_404(FeedbackStudent, id=feedback_id)
            reply = request.POST.get('reply')
            feedback.reply = reply
            feedback.save()
            return HttpResponse(True)
        except Exception as e:
            logger.exception('Unhandled error in student_feedback_message')
            return HttpResponse(False)


def staff_feedback_message(request):
    if request.method != 'POST':
        feedbacks = paginate(request, FeedbackStaff.objects.select_related('staff').order_by('id'))
        classes_by_staff = staff_class_names_map([f.staff_id for f in feedbacks])
        for feedback in feedbacks:
            feedback.class_names = classes_by_staff.get(feedback.staff_id, '—')
        context = {
            'feedbacks': feedbacks,
            'page_obj': feedbacks,
            'page_title': 'Staff Feedback Messages'
        }
        return render(request, 'hod_template/staff_feedback_template.html', context)
    else:
        feedback_id = request.POST.get('id')
        try:
            feedback = get_object_or_404(FeedbackStaff, id=feedback_id)
            reply = request.POST.get('reply')
            feedback.reply = reply
            feedback.save()
            return HttpResponse(True)
        except Exception as e:
            logger.exception('Unhandled error in staff_feedback_message')
            return HttpResponse(False)


def view_staff_leave(request):
    if request.method != 'POST':
        allLeave = paginate(request, LeaveReportStaff.objects.select_related('staff').order_by('id'))
        classes_by_staff = staff_class_names_map([leave.staff_id for leave in allLeave])
        for leave in allLeave:
            leave.class_names = classes_by_staff.get(leave.staff_id, '—')
        context = {
            'allLeave': allLeave,
            'page_obj': allLeave,
            'page_title': 'Leave Applications From Staff'
        }
        return render(request, "hod_template/staff_leave_view.html", context)
    else:
        id = request.POST.get('id')
        status = request.POST.get('status')
        if (status == '1'):
            status = 1
        else:
            status = -1
        try:
            leave = get_object_or_404(LeaveReportStaff, id=id)
            if leave.status != 0:
                # Already decided - don't overwrite or send a duplicate notification.
                return HttpResponse(False)
            leave.status = status
            leave.save()
            message = leave_decision_message(leave.date, status)
            NotificationStaff.objects.create(staff=leave.staff, message=message)
            send_notification_email(leave.staff.admin, message)
            return HttpResponse(True)
        except Exception as e:
            logger.exception("Failed to update staff leave status")
            return HttpResponse(False)


def view_student_leave(request):
    if request.method != 'POST':
        allLeave = paginate(request, LeaveReportStudent.objects.order_by('id'))
        context = {
            'allLeave': allLeave,
            'page_obj': allLeave,
            'page_title': 'Leave Applications From Students'
        }
        return render(request, "hod_template/student_leave_view.html", context)
    else:
        id = request.POST.get('id')
        status = request.POST.get('status')
        if (status == '1'):
            status = 1
        else:
            status = -1
        try:
            leave = get_object_or_404(LeaveReportStudent, id=id)
            if leave.status != 0:
                # Already decided - don't overwrite or send a duplicate notification.
                return HttpResponse(False)
            leave.status = status
            leave.save()
            notify_student_leave_decision(leave, status)
            return HttpResponse(True)
        except Exception as e:
            logger.exception("Failed to update student leave status")
            return HttpResponse(False)


def view_parent_link_requests(request):
    if request.method != 'POST':
        allLinks = paginate(request, ParentStudentLink.objects.select_related(
            'parent__admin', 'student__admin').order_by('id'))
        # More than one PENDING request for the same student is exactly the
        # case an admin most needs to notice - e.g. two different accounts
        # claiming the same kid with mismatched self-reported DOBs - so flag
        # it rather than relying on spotting it by eye across a paginated list.
        pending_student_counts = {}
        for student_id in ParentStudentLink.objects.filter(status=0).values_list('student_id', flat=True):
            pending_student_counts[student_id] = pending_student_counts.get(student_id, 0) + 1
        contested_student_ids = {sid for sid, count in pending_student_counts.items() if count > 1}
        for link in allLinks:
            link.is_contested = link.status == 0 and link.student_id in contested_student_ids
        context = {
            'allLinks': allLinks,
            'page_obj': allLinks,
            'page_title': 'Parent Link Requests'
        }
        return render(request, "hod_template/parent_link_view.html", context)
    else:
        id = request.POST.get('id')
        status = request.POST.get('status')
        status = 1 if status == '1' else -1
        try:
            with transaction.atomic():
                # select_for_update so two near-simultaneous approve/reject
                # requests for the same link can't both pass the status==0
                # check before either writes (duplicate notification/email).
                link = get_object_or_404(
                    ParentStudentLink.objects.select_for_update(), id=id)
                if link.status != 0:
                    # Already decided - don't overwrite or send a duplicate notification.
                    return HttpResponse(False)
                link.status = status
                link.decided_at = timezone.now()
                link.decided_by = request.user
                link.save()
            if status == 1 and not link.student.date_of_birth:
                link.student.date_of_birth = link.date_of_birth
                link.student.save()
            message = parent_link_decision_message(link.student, status)
            NotificationParent.objects.create(parent=link.parent, message=message)
            send_notification_email(link.parent.admin, message)
            send_push_notification(link.parent.admin, message, 'parent_view_notification')
            log_action(request, 'approved' if status == 1 else 'rejected', 'ParentStudentLink', link)
            return HttpResponse(True)
        except Exception:
            logger.exception("Failed to update parent link status")
            return HttpResponse(False)


def admin_view_attendance(request):
    courses = Course.objects.order_by('name')
    subjects = Subject.objects.select_related('course').order_by('course__name', 'name')
    sessions = Session.objects.order_by('-start_year')
    session_courses = session_course_ids_map()
    for session in sessions:
        session.course_ids_str = ' '.join(str(c) for c in session_courses.get(session.id, []))
    context = {
        'courses': courses,
        'subjects': subjects,
        'sessions': sessions,
        'page_title': 'View Attendance'
    }

    return render(request, "hod_template/admin_view_attendance.html", context)


def get_admin_attendance(request):
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    attendance_date_id = request.POST.get('attendance_date_id')
    try:
        subject = get_object_or_404(Subject, id=subject_id)
        session = get_object_or_404(Session, id=session_id)
        attendance = get_object_or_404(
            Attendance, id=attendance_date_id, session=session)
        # The full class roster, not just students with an existing
        # AttendanceReport - a student on approved leave never gets one,
        # but should still be visible here rather than disappearing.
        students = Student.objects.filter(course_id=subject.course_id, session=session)
        reports_by_student = {
            r.student_id: r for r in AttendanceReport.objects.filter(attendance=attendance)
        }
        on_leave_ids = set(
            LeaveReportStudent.objects.filter(
                student__in=students, date=attendance.date.isoformat(), status=1
            ).values_list('student_id', flat=True)
        )
        json_data = []
        for student in students:
            report = reports_by_student.get(student.id)
            status = "On Leave" if student.id in on_leave_ids else str(bool(report.status)) if report else "Not Recorded"
            json_data.append({"status": status, "name": str(student)})
        return JsonResponse(json.dumps(json_data), safe=False)
    except Exception as e:
        logger.exception("Failed to fetch admin attendance")
        return JsonResponse({'error': str(e)}, status=400)


def admin_view_profile(request):
    admin = get_object_or_404(Admin, admin=request.user)
    form = AdminForm(request.POST or None, request.FILES or None,
                     instance=admin)
    context = {'form': form,
               'page_title': 'View/Edit Profile'
               }
    if request.method == 'POST':
        try:
            if form.is_valid():
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                password = form.cleaned_data.get('password') or None
                passport = request.FILES.get('profile_pic') or None
                custom_user = admin.admin
                if password is not None:
                    custom_user.set_password(password)
                if passport is not None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    custom_user.profile_pic = passport_url
                custom_user.first_name = first_name
                custom_user.last_name = last_name
                custom_user.save()
                messages.success(request, "Profile Updated!")
                return redirect(reverse('admin_view_profile'))
            else:
                messages.error(request, "Please check the form - some required fields are missing or invalid.")
        except Exception as e:
            logger.exception('Unhandled error in admin_view_profile')
            messages.error(
                request, "Error Occurred While Updating Profile " + str(e))
    return render(request, "hod_template/admin_view_profile.html", context)


def admin_notify_staff(request):
    staff = CustomUser.objects.filter(staff__isnull=False).select_related('staff')
    classes_by_staff = staff_class_names_map([user.staff.id for user in staff])
    for user in staff:
        user.class_names = classes_by_staff.get(user.staff.id, '—')
    context = {
        'page_title': "Send Notifications To Staff",
        'allStaff': staff
    }
    return render(request, "hod_template/staff_notification.html", context)


def admin_notify_student(request):
    student = CustomUser.objects.filter(student__isnull=False)
    context = {
        'page_title': "Send Notifications To Students",
        'students': student
    }
    return render(request, "hod_template/student_notification.html", context)


def admin_notify_parent(request):
    parents = CustomUser.objects.filter(parent__isnull=False).select_related('parent')
    links_by_parent = {}
    for link in ParentStudentLink.objects.filter(parent__admin__in=parents, status=1).select_related('student__admin'):
        links_by_parent.setdefault(link.parent_id, []).append(link)
    for user in parents:
        user.links = links_by_parent.get(user.parent.id, [])
    context = {
        'page_title': "Send Notifications To Parents",
        'parents': parents
    }
    return render(request, "hod_template/parent_notification.html", context)


def send_student_notification(request):
    id = request.POST.get('id')
    message = request.POST.get('message')
    student = get_object_or_404(Student, admin_id=id)
    try:
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': "Student Management System",
                'body': message,
                'click_action': reverse('student_view_notification'),
                'icon': static('dist/img/AdminLTELogo.png')
            },
            'to': student.admin.fcm_token
        }
        headers = {'Authorization': 'key=' + settings.FCM_SERVER_KEY,
                   'Content-Type': 'application/json'}
        data = requests.post(url, data=json.dumps(body), headers=headers)
        notification = NotificationStudent(student=student, message=message)
        notification.save()
        send_notification_email(student.admin, message)
        return HttpResponse("True")
    except Exception as e:
        logger.exception('Unhandled error in send_student_notification')
        return HttpResponse("False")


def send_parent_notification(request):
    id = request.POST.get('id')
    message = request.POST.get('message')
    parent = get_object_or_404(Parent, admin_id=id)
    try:
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': "Student Management System",
                'body': message,
                'click_action': reverse('parent_view_notification'),
                'icon': static('dist/img/AdminLTELogo.png')
            },
            'to': parent.admin.fcm_token
        }
        headers = {'Authorization': 'key=' + settings.FCM_SERVER_KEY,
                   'Content-Type': 'application/json'}
        data = requests.post(url, data=json.dumps(body), headers=headers)
        notification = NotificationParent(parent=parent, message=message)
        notification.save()
        send_notification_email(parent.admin, message)
        return HttpResponse("True")
    except Exception as e:
        logger.exception('Unhandled error in send_parent_notification')
        return HttpResponse("False")


def send_staff_notification(request):
    id = request.POST.get('id')
    message = request.POST.get('message')
    staff = get_object_or_404(Staff, admin_id=id)
    try:
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': "Student Management System",
                'body': message,
                'click_action': reverse('staff_view_notification'),
                'icon': static('dist/img/AdminLTELogo.png')
            },
            'to': staff.admin.fcm_token
        }
        headers = {'Authorization': 'key=' + settings.FCM_SERVER_KEY,
                   'Content-Type': 'application/json'}
        data = requests.post(url, data=json.dumps(body), headers=headers)
        notification = NotificationStaff(staff=staff, message=message)
        notification.save()
        send_notification_email(staff.admin, message)
        return HttpResponse("True")
    except Exception as e:
        logger.exception('Unhandled error in send_staff_notification')
        return HttpResponse("False")


def toggle_staff_status(request, staff_id):
    staff_user = get_object_or_404(CustomUser, staff__id=staff_id)
    staff_user.is_active = not staff_user.is_active
    staff_user.save()
    log_action(request, 'activated' if staff_user.is_active else 'deactivated', 'Staff', staff_user)
    messages.success(
        request, f"Staff {'activated' if staff_user.is_active else 'deactivated'} successfully!")
    return redirect(reverse('manage_staff'))


def toggle_student_status(request, student_id):
    student_user = get_object_or_404(CustomUser, student__id=student_id)
    student_user.is_active = not student_user.is_active
    student_user.save()
    log_action(request, 'activated' if student_user.is_active else 'deactivated', 'Student', student_user)
    messages.success(
        request, f"Student {'activated' if student_user.is_active else 'deactivated'} successfully!")
    return redirect(reverse('manage_student'))


def toggle_admin_status(request, admin_id):
    admin_user = get_object_or_404(CustomUser, admin__id=admin_id)
    if admin_user.id == request.user.id:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect(reverse('manage_admin'))
    admin_user.is_active = not admin_user.is_active
    admin_user.save()
    log_action(request, 'activated' if admin_user.is_active else 'deactivated', 'Admin', admin_user)
    messages.success(
        request, f"Admin {'activated' if admin_user.is_active else 'deactivated'} successfully!")
    return redirect(reverse('manage_admin'))


def delete_admin(request, admin_id):
    admin = get_object_or_404(Admin, id=admin_id)
    user = admin.admin
    if user.id == request.user.id:
        messages.error(request, "You cannot delete your own account.")
        return redirect(reverse('manage_admin'))
    try:
        admin_repr = str(user)
        # Only this role's own row - a person who also holds another role
        # (e.g. Staff) must keep that role/account intact. The CustomUser
        # itself only goes away once no role is left on it at all.
        admin.delete()
        # Re-fetch rather than reuse `user` - accessing admin.admin above
        # cached the (now stale) reverse "admin" relation onto it, so
        # user_roles(user) would still see the just-deleted role otherwise.
        if not user_roles(CustomUser.objects.get(pk=user.pk)):
            user.delete()
        log_action(request, 'deleted', 'Admin', admin_repr)
        messages.success(request, "Admin deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_admin')
        messages.error(request, "Could not delete this admin.")
    return redirect(reverse('manage_admin'))


def toggle_parent_status(request, parent_id):
    parent_user = get_object_or_404(CustomUser, parent__id=parent_id)
    parent_user.is_active = not parent_user.is_active
    parent_user.save()
    log_action(request, 'activated' if parent_user.is_active else 'deactivated', 'Parent', parent_user)
    messages.success(
        request, f"Parent {'activated' if parent_user.is_active else 'deactivated'} successfully!")
    return redirect(reverse('manage_parent'))


def delete_parent(request, parent_id):
    parent = get_object_or_404(Parent, id=parent_id)
    user = parent.admin
    try:
        parent_repr = str(user)
        parent.delete()
        if not user_roles(CustomUser.objects.get(pk=user.pk)):
            user.delete()
        log_action(request, 'deleted', 'Parent', parent_repr)
        messages.success(request, "Parent deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_parent')
        messages.error(request, "Could not delete this parent.")
    return redirect(reverse('manage_parent'))


def delete_staff(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    user = staff.admin
    try:
        staff_repr = str(user)
        staff.delete()
        if not user_roles(CustomUser.objects.get(pk=user.pk)):
            user.delete()
        log_action(request, 'deleted', 'Staff', staff_repr)
        messages.success(request, "Staff deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_staff')
        messages.error(request, "Could not delete this staff member.")
    return redirect(reverse('manage_staff'))


def delete_student(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    user = student.admin
    try:
        student_repr = str(user)
        student.delete()
        if not user_roles(CustomUser.objects.get(pk=user.pk)):
            user.delete()
        log_action(request, 'deleted', 'Student', student_repr)
        messages.success(request, "Student deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_student')
        messages.error(request, "Could not delete this student.")
    return redirect(reverse('manage_student'))


def delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    try:
        course_repr = str(course)
        course.delete()
        log_action(request, 'deleted', 'Class', course_repr)
        messages.success(request, "Class deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_course')
        messages.error(
            request, "Sorry, some students are assigned to this class already. Kindly change the affected student's class and try again")
    return redirect(reverse('manage_course'))


def delete_subject(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    try:
        subject_repr = str(subject)
        subject.delete()
        log_action(request, 'deleted', 'Subject', subject_repr)
        messages.success(request, "Subject deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_subject')
        messages.error(request, "Could not delete this subject.")
    return redirect(reverse('manage_subject'))


def delete_session(request, session_id):
    session = get_object_or_404(Session, id=session_id)
    try:
        session_repr = str(session)
        session.delete()
        log_action(request, 'deleted', 'Session', session_repr)
        messages.success(request, "Session deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_session')
        messages.error(
            request, "There are students assigned to this session. Please move them to another session.")
    return redirect(reverse('manage_session'))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _class_subjects_report_data(request):
    course_id = request.GET.get('course') or ''
    staff_id = request.GET.get('staff') or ''
    subjects = Subject.objects.select_related('course', 'staff__admin').order_by('course__name', 'name')
    if course_id:
        subjects = subjects.filter(course_id=course_id)
    if staff_id:
        subjects = subjects.filter(staff_id=staff_id)
    return {'course_id': course_id, 'staff_id': staff_id, 'subjects': subjects}


def report_class_subjects(request):
    data = _class_subjects_report_data(request)
    context = {
        'page_title': 'Class & Subject Assignments',
        'courses': Course.objects.order_by('name'),
        'staff_list': Staff.objects.select_related('admin').order_by('admin__first_name'),
        'selected_course': data['course_id'],
        'selected_staff': data['staff_id'],
        'subjects': data['subjects'],
    }
    return render(request, 'hod_template/report_class_subjects.html', context)


def report_class_subjects_csv(request):
    data = _class_subjects_report_data(request)
    rows = [(s.course.name, s.name, str(s.staff)) for s in data['subjects']]
    return csv_response('class_subject_assignments.csv', ['Class', 'Subject', 'Teacher'], rows)


def _exclude_approved_leave(reports):
    """Drop AttendanceReport rows that fall on a date the student had an
    approved leave application for, so a leave day counts neither as
    present nor absent in attendance-percentage/chronic-absence math.
    LeaveReportStudent.date is a free-text CharField (fed by an HTML5
    date input, so it's a 'YYYY-MM-DD' string), while Attendance.date is
    a real DateField - compare them as strings in Python rather than
    relying on the DB to coerce mismatched column types."""
    approved_leave_days = set(
        LeaveReportStudent.objects.filter(status=1).values_list('student_id', 'date')
    )
    if not approved_leave_days:
        return reports
    excluded_ids = [
        row['id'] for row in reports.values('id', 'student_id', leave_date=F('attendance__date'))
        if (row['student_id'], row['leave_date'].isoformat()) in approved_leave_days
    ]
    return reports.exclude(id__in=excluded_ids) if excluded_ids else reports


def _approved_leave_counts(course_id, session_id, start_date, end_date):
    """Approved-leave day counts grouped by course name, date, and
    student, scoped the same way as the attendance queryset - so the
    reports can show a Leave column even though a leave day never gets
    an AttendanceReport row of its own."""
    leaves = LeaveReportStudent.objects.filter(
        status=1, date__gte=start_date, date__lte=end_date)
    if course_id:
        leaves = leaves.filter(student__course_id=course_id)
    if session_id:
        leaves = leaves.filter(student__session_id=session_id)

    by_course = dict(
        leaves.values('student__course__name')
        .annotate(count=Count('id')).values_list('student__course__name', 'count')
    )
    by_day = dict(
        leaves.values('date')
        .annotate(count=Count('id')).values_list('date', 'count')
    )
    by_student = dict(
        leaves.values('student_id')
        .annotate(count=Count('id')).values_list('student_id', 'count')
    )
    return by_course, by_day, by_student


def _attendance_summary_data(request):
    course_id = request.GET.get('course') or ''
    session_id = request.GET.get('session') or ''
    subject_id = request.GET.get('subject') or ''
    start_date = request.GET.get('start_date') or date.today().replace(day=1).isoformat()
    end_date = request.GET.get('end_date') or date.today().isoformat()

    reports = AttendanceReport.objects.filter(
        attendance__date__gte=start_date, attendance__date__lte=end_date)
    if course_id:
        reports = reports.filter(attendance__subject__course_id=course_id)
    if session_id:
        reports = reports.filter(attendance__session_id=session_id)
    if subject_id:
        reports = reports.filter(attendance__subject_id=subject_id)
    reports = _exclude_approved_leave(reports)

    leave_by_course, leave_by_day, leave_by_student = _approved_leave_counts(
        course_id, session_id, start_date, end_date)

    def with_percent(rows, key, leave_lookup, key_to_lookup=lambda v: v):
        summary = []
        for row in rows:
            total = row['total']
            present = row['present']
            summary.append({
                key: row[key],
                'total': total,
                'present': present,
                'absent': total - present,
                'percent': round(present / total * 100, 1) if total else 0,
                'leave': leave_lookup.get(key_to_lookup(row[key]), 0),
            })
        return summary

    by_course = (
        reports.values(course=F('attendance__subject__course__name'))
        .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
        .order_by('course')
    )
    by_day = (
        reports.values(date=F('attendance__date'))
        .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
        .order_by('date')
    )
    by_subject_raw = (
        reports.values(
            subject=F('attendance__subject__name'),
            course=F('attendance__subject__course__name'),
        )
        .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
        .order_by('course', 'subject')
    )
    subject_summary = []
    for row in by_subject_raw:
        total = row['total']
        present = row['present']
        subject_summary.append({
            'course': row['course'],
            'subject': row['subject'],
            'total': total,
            'present': present,
            'absent': total - present,
            'percent': round(present / total * 100, 1) if total else 0,
            # Leave isn't a subject-specific concept - an approved leave
            # exempts a student from every subject that day - so this is
            # the same course-level count shown in "By Course", attached
            # to each of that course's subjects for context.
            'leave': leave_by_course.get(row['course'], 0),
        })
    by_student_raw = (
        reports.values('student_id', 'student__admin__first_name', 'student__admin__last_name')
        .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
    )
    student_summary = []
    seen_student_ids = set()
    for row in by_student_raw:
        total = row['total']
        present = row['present']
        percent = round(present / total * 100, 1) if total else 0
        seen_student_ids.add(row['student_id'])
        student_summary.append({
            'name': f"{row['student__admin__last_name']}, {row['student__admin__first_name']}",
            'total': total,
            'present': present,
            'absent': total - present,
            'percent': percent,
            'leave': leave_by_student.get(row['student_id'], 0),
            'flagged': percent < CHRONIC_ABSENCE_THRESHOLD,
        })
    # A student whose only records in range were approved leave has no
    # AttendanceReport rows left after exclusion, so they'd otherwise be
    # missing from this table entirely - add them with 0 total, so the
    # leave column stays visible instead of the student just vanishing.
    # Not flagged: they were never actually absent.
    leave_only_ids = set(leave_by_student) - seen_student_ids
    if leave_only_ids:
        for student in Student.objects.filter(id__in=leave_only_ids).select_related('admin'):
            student_summary.append({
                'name': str(student),
                'total': 0,
                'present': 0,
                'absent': 0,
                'percent': 0,
                'leave': leave_by_student[student.id],
                'flagged': False,
            })
    # Worst attendance first, so chronic absences surface immediately.
    student_summary.sort(key=lambda r: r['percent'])

    return {
        'course_id': course_id,
        'session_id': session_id,
        'subject_id': subject_id,
        'start_date': start_date,
        'end_date': end_date,
        'course_summary': with_percent(by_course, 'course', leave_by_course),
        'daily_summary': with_percent(by_day, 'date', leave_by_day, key_to_lookup=lambda d: d.isoformat()),
        'subject_summary': subject_summary,
        'student_summary': student_summary,
    }


def report_attendance_summary(request):
    data = _attendance_summary_data(request)
    sessions = Session.objects.order_by('-start_year')
    session_courses = session_course_ids_map()
    for session in sessions:
        session.course_ids_str = ' '.join(str(c) for c in session_courses.get(session.id, []))
    context = {
        'page_title': 'Attendance Summary Report',
        'courses': Course.objects.order_by('name'),
        'sessions': sessions,
        'subjects': Subject.objects.select_related('course').order_by('course__name', 'name'),
        'selected_course': data['course_id'],
        'selected_session': data['session_id'],
        'selected_subject': data['subject_id'],
        'start_date': data['start_date'],
        'end_date': data['end_date'],
        'course_summary': data['course_summary'],
        'daily_summary': data['daily_summary'],
        'subject_summary': data['subject_summary'],
        'student_summary': data['student_summary'],
        'chronic_threshold': CHRONIC_ABSENCE_THRESHOLD,
    }
    return render(request, 'hod_template/report_attendance_summary.html', context)


def report_attendance_summary_csv(request):
    data = _attendance_summary_data(request)
    rows = [(row['course'], row['total'], row['present'], row['absent'], row['leave'], f"{row['percent']}%")
            for row in data['course_summary']]
    filename = f"attendance_summary_{data['start_date']}_to_{data['end_date']}.csv"
    return csv_response(filename, ['Class', 'Total', 'Present', 'Absent', 'Leave', 'Attendance %'], rows)


def report_attendance_by_subject_csv(request):
    data = _attendance_summary_data(request)
    rows = [(row['course'], row['subject'], row['total'], row['present'], row['absent'], row['leave'],
             f"{row['percent']}%") for row in data['subject_summary']]
    filename = f"attendance_by_subject_{data['start_date']}_to_{data['end_date']}.csv"
    return csv_response(filename, ['Class', 'Subject', 'Total', 'Present', 'Absent', 'Leave', 'Attendance %'], rows)


def report_attendance_by_student_csv(request):
    data = _attendance_summary_data(request)
    rows = [(row['name'], row['total'], row['present'], row['absent'], row['leave'], f"{row['percent']}%",
             'Yes' if row['flagged'] else '') for row in data['student_summary']]
    filename = f"attendance_by_student_{data['start_date']}_to_{data['end_date']}.csv"
    return csv_response(filename, ['Student', 'Total', 'Present', 'Absent', 'Leave', 'Attendance %', 'Below Threshold'], rows)


def report_student_attendance(request):
    student_id = request.GET.get('student') or ''
    start_date = request.GET.get('start_date') or ''
    end_date = request.GET.get('end_date') or ''

    records = []
    summary = None
    subject_summary = []
    if student_id:
        student = get_object_or_404(Student, id=student_id)
        reports = AttendanceReport.objects.filter(student=student).select_related(
            'attendance', 'attendance__subject')
        if start_date:
            reports = reports.filter(attendance__date__gte=start_date)
        if end_date:
            reports = reports.filter(attendance__date__lte=end_date)
        reports = reports.order_by('-attendance__date')
        # Leave days stay visible in the raw history below, but don't
        # count toward the present/absent percentage.
        counted = _exclude_approved_leave(reports)
        total = counted.count()
        present = counted.filter(status=True).count()
        leave_qs = LeaveReportStudent.objects.filter(student=student, status=1)
        if start_date:
            leave_qs = leave_qs.filter(date__gte=start_date)
        if end_date:
            leave_qs = leave_qs.filter(date__lte=end_date)
        summary = {
            'student': student,
            'total': total,
            'present': present,
            'absent': total - present,
            'leave': leave_qs.count(),
            'percent': round(present / total * 100, 1) if total else 0,
        }
        records = reports

        by_subject_raw = (
            counted.values(subject=F('attendance__subject__name'))
            .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
            .order_by('subject')
        )
        for row in by_subject_raw:
            row_total = row['total']
            row_present = row['present']
            subject_summary.append({
                'subject': row['subject'],
                'total': row_total,
                'present': row_present,
                'absent': row_total - row_present,
                'percent': round(row_present / row_total * 100, 1) if row_total else 0,
            })

    context = {
        'page_title': 'Student Attendance History',
        'students': Student.objects.select_related('admin').order_by('admin__first_name'),
        'selected_student': student_id,
        'start_date': start_date,
        'end_date': end_date,
        'records': records,
        'summary': summary,
        'subject_summary': subject_summary,
    }
    return render(request, 'hod_template/report_student_attendance.html', context)


def report_student_attendance_csv(request):
    student_id = request.GET.get('student') or ''
    if not student_id:
        return HttpResponse("Select a student first.", status=400)
    student = get_object_or_404(Student, id=student_id)
    reports = AttendanceReport.objects.filter(student=student).select_related(
        'attendance', 'attendance__subject')
    start_date = request.GET.get('start_date') or ''
    end_date = request.GET.get('end_date') or ''
    if start_date:
        reports = reports.filter(attendance__date__gte=start_date)
    if end_date:
        reports = reports.filter(attendance__date__lte=end_date)
    reports = reports.order_by('-attendance__date')

    rows = [(r.attendance.date, r.attendance.subject.name, "Present" if r.status else "Absent")
            for r in reports]
    filename = f"attendance_{student.admin.first_name}_{student.admin.last_name}.csv"
    return csv_response(filename, ['Date', 'Subject', 'Status'], rows)


def _leave_report_data(request):
    leave_type = request.GET.get('type') or 'all'
    status = request.GET.get('status') or 'all'
    course_id = request.GET.get('course') or ''
    start_date = request.GET.get('start_date') or ''
    end_date = request.GET.get('end_date') or ''

    status_map = {'pending': 0, 'approved': 1, 'rejected': -1}

    student_leaves = LeaveReportStudent.objects.select_related('student', 'student__admin', 'student__course')
    staff_leaves = LeaveReportStaff.objects.select_related('staff', 'staff__admin')
    if status in status_map:
        student_leaves = student_leaves.filter(status=status_map[status])
        staff_leaves = staff_leaves.filter(status=status_map[status])
    if start_date:
        student_leaves = student_leaves.filter(date__gte=start_date)
        staff_leaves = staff_leaves.filter(date__gte=start_date)
    if end_date:
        student_leaves = student_leaves.filter(date__lte=end_date)
        staff_leaves = staff_leaves.filter(date__lte=end_date)
    if course_id:
        student_leaves = student_leaves.filter(student__course_id=course_id)
        # A staff member has no single class - "in this class" means they
        # teach at least one subject there.
        staff_leaves = staff_leaves.filter(staff__subject__course_id=course_id).distinct()

    # A staff member's classes are every course they teach a subject in
    # (see teacher_course_ids), not a single field - look them all up once.
    staff_classes = staff_class_names_map([l.staff_id for l in staff_leaves])

    records = []
    if leave_type in ('all', 'student'):
        records += [{
            'type': 'Student', 'name': str(l.student), 'date': l.date,
            'class_names': l.student.course.name if l.student.course_id else '—',
            'message': l.message, 'status_label': LEAVE_STATUS_LABELS.get(l.status, 'Unknown'),
        } for l in student_leaves]
    if leave_type in ('all', 'staff'):
        records += [{
            'type': 'Staff', 'name': str(l.staff), 'date': l.date,
            'class_names': staff_classes.get(l.staff_id, '—'),
            'message': l.message, 'status_label': LEAVE_STATUS_LABELS.get(l.status, 'Unknown'),
        } for l in staff_leaves]
    records.sort(key=lambda r: r['date'], reverse=True)

    return {
        'leave_type': leave_type, 'status': status, 'course_id': course_id,
        'start_date': start_date, 'end_date': end_date,
        'records': records,
    }


def report_leave(request):
    data = _leave_report_data(request)
    context = {
        'page_title': 'Leave Report',
        'courses': Course.objects.order_by('name'),
        'records': data['records'],
        'selected_type': data['leave_type'],
        'selected_status': data['status'],
        'selected_course': data['course_id'],
        'start_date': data['start_date'],
        'end_date': data['end_date'],
    }
    return render(request, 'hod_template/report_leave.html', context)


def report_leave_csv(request):
    data = _leave_report_data(request)
    rows = [(r['type'], r['name'], r['class_names'], r['date'], r['status_label'], r['message'])
            for r in data['records']]
    return csv_response('leave_report.csv', ['Type', 'Name', 'Class', 'Date', 'Status', 'Message'], rows)


def _results_report_data(request):
    course_id = request.GET.get('course') or ''
    subject_id = request.GET.get('subject') or ''

    results = StudentResult.objects.select_related('student__admin', 'subject', 'subject__course')
    if course_id:
        results = results.filter(subject__course_id=course_id)
    if subject_id:
        results = results.filter(subject_id=subject_id)
    results = results.order_by('subject__course__name', 'subject__name', 'student__admin__first_name')

    summary = None
    agg = results.aggregate(avg_test=Avg('test'), avg_exam=Avg('exam'), count=Count('id'))
    if agg['count']:
        summary = {
            'count': agg['count'],
            'avg_test': round(agg['avg_test'], 1),
            'avg_exam': round(agg['avg_exam'], 1),
        }

    return {'course_id': course_id, 'subject_id': subject_id, 'results': results, 'summary': summary}


def report_results(request):
    data = _results_report_data(request)
    context = {
        'page_title': 'Results Summary Report',
        'courses': Course.objects.order_by('name'),
        'subjects': Subject.objects.select_related('course').order_by('course__name', 'name'),
        'selected_course': data['course_id'],
        'selected_subject': data['subject_id'],
        'results': data['results'],
        'summary': data['summary'],
    }
    return render(request, 'hod_template/report_results.html', context)


def report_results_csv(request):
    data = _results_report_data(request)
    rows = [(r.subject.course.name, r.subject.name, str(r.student), r.test, r.exam)
            for r in data['results']]
    return csv_response('results_report.csv', ['Class', 'Subject', 'Student', 'Test', 'Exam'], rows)


def report_activity_log(request):
    logs = paginate(request, AuditLog.objects.select_related('actor'))
    context = {
        'page_title': 'Activity Log',
        'logs': logs,
        'page_obj': logs,
    }
    return render(request, 'hod_template/report_activity_log.html', context)


def report_activity_log_csv(request):
    logs = AuditLog.objects.select_related('actor')
    rows = [(log.created_at.strftime('%Y-%m-%d %H:%M'), str(log.actor) if log.actor else 'Unknown',
              log.action, log.target_model, log.target_repr) for log in logs]
    return csv_response('activity_log.csv', ['Timestamp', 'Actor', 'Action', 'Target Type', 'Target'], rows)
