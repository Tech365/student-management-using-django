import json
import logging
from datetime import date, datetime, timedelta

import requests
from django.conf import settings
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.db.models import Avg, Count, F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.crypto import get_random_string

from .forms import (AdminForm, CourseForm, CSVUploadForm, SessionForm,
                    StaffForm, StudentForm, SubjectForm)
from .models import (Admin, AttendanceReport, Attendance, AuditLog, Course,
                     CustomUser, FeedbackStaff, FeedbackStudent,
                     LeaveReportStaff, LeaveReportStudent, NotificationStaff,
                     NotificationStudent, Session, Staff, Student,
                     StudentResult, Subject)
from .utils import (csv_response, log_action, paginate, read_csv_rows,
                    send_notification_email)

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
        'subject_list': subject_list,
        'attendance_list': attendance_list

    }
    return render(request, 'hod_template/home_content.html', context)


def add_admin(request):
    form = AdminForm(request.POST or None, request.FILES or None)
    context = {'form': form, 'page_title': 'Add Admin'}
    if request.method == 'POST':
        if form.is_valid():
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
                messages.error(request, "Could Not Add " + str(e))
        else:
            messages.error(request, "Please fulfil all requirements")

    return render(request, 'hod_template/add_admin_template.html', context)


def add_staff(request):
    form = StaffForm(request.POST or None, request.FILES or None)
    context = {'form': form, 'page_title': 'Add Staff'}
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            email = form.cleaned_data.get('email')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password')
            course = form.cleaned_data.get('course')
            passport = request.FILES.get('profile_pic')
            fs = FileSystemStorage()
            filename = fs.save(passport.name, passport)
            passport_url = fs.url(filename)
            try:
                user = CustomUser.objects.create_user(
                    email=email, password=password, user_type=2, first_name=first_name, last_name=last_name, profile_pic=passport_url)
                user.gender = gender
                user.address = address
                user.staff.course = course
                user.save()
                log_action(request, 'created', 'Staff', user)
                messages.success(request, "Successfully Added")
                return redirect(reverse('add_staff'))

            except Exception as e:
                logger.exception('Unhandled error in add_staff')
                messages.error(request, "Could Not Add " + str(e))
        else:
            messages.error(request, "Please fulfil all requirements")

    return render(request, 'hod_template/add_staff_template.html', context)


def add_student(request):
    student_form = StudentForm(request.POST or None, request.FILES or None)
    context = {'form': student_form, 'page_title': 'Add Student'}
    if request.method == 'POST':
        if student_form.is_valid():
            first_name = student_form.cleaned_data.get('first_name')
            last_name = student_form.cleaned_data.get('last_name')
            address = student_form.cleaned_data.get('address')
            email = student_form.cleaned_data.get('email')
            gender = student_form.cleaned_data.get('gender')
            password = student_form.cleaned_data.get('password')
            course = student_form.cleaned_data.get('course')
            session = student_form.cleaned_data.get('session')
            passport = request.FILES['profile_pic']
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
                messages.error(request, "Could Not Add: " + str(e))
        else:
            messages.error(request, "Could Not Add: ")
    return render(request, 'hod_template/add_student_template.html', context)


def add_course(request):
    form = CourseForm(request.POST or None)
    context = {
        'form': form,
        'page_title': 'Add Course'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            try:
                course = Course()
                course.name = name
                course.save()
                log_action(request, 'created', 'Course', course)
                messages.success(request, "Successfully Added")
                return redirect(reverse('add_course'))
            except:
                logger.exception('Unhandled error in add_course')
                messages.error(request, "Could Not Add")
        else:
            messages.error(request, "Could Not Add")
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
                messages.error(request, "Could Not Add " + str(e))
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
    context = {'form': form, 'results': results, 'page_title': 'Bulk Upload Courses'}
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
                course_name = row.get('course') or ''
                missing = [f for f, v in [('name', name), ('staff_email', staff_email), ('course', course_name)] if not v]
                if missing:
                    results.append({'row': row_number, 'status': 'error', 'message': f"Missing {', '.join(missing)}"})
                    continue
                staff = Staff.objects.filter(admin__email__iexact=staff_email).first()
                if staff is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No staff with email "{staff_email}"'})
                    continue
                course = Course.objects.filter(name__iexact=course_name).first()
                if course is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No course named "{course_name}"'})
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
                course_name = row.get('course') or ''

                missing = [f for f, v in [('first_name', first_name), ('last_name', last_name),
                                           ('email', email), ('gender', gender), ('course', course_name)] if not v]
                if missing:
                    results.append({'row': row_number, 'status': 'error', 'message': f"Missing {', '.join(missing)}"})
                    continue
                if gender not in ('M', 'F'):
                    results.append({'row': row_number, 'status': 'error', 'message': f'Gender must be M or F, got "{gender}"'})
                    continue
                if CustomUser.objects.filter(email=email).exists():
                    results.append({'row': row_number, 'status': 'skipped', 'message': f'"{email}" already registered'})
                    continue
                course = Course.objects.filter(name__iexact=course_name).first()
                if course is None:
                    results.append({'row': row_number, 'status': 'error', 'message': f'No course named "{course_name}"'})
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
                    user.staff.course = course
                    user.staff.save()
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
                course_name = row.get('course') or ''
                session_start = row.get('session_start_year') or ''
                session_end = row.get('session_end_year') or ''

                missing = [f for f, v in [('first_name', first_name), ('last_name', last_name),
                                           ('email', email), ('gender', gender), ('course', course_name),
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
                    results.append({'row': row_number, 'status': 'error', 'message': f'No course named "{course_name}"'})
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
    admins = paginate(request, CustomUser.objects.filter(user_type=1).select_related('admin').order_by('id'))
    context = {
        'admins': admins,
        'page_obj': admins,
        'page_title': 'Manage Admins'
    }
    return render(request, "hod_template/manage_admin.html", context)


def manage_staff(request):
    allStaff = paginate(request, CustomUser.objects.filter(user_type=2).select_related('staff', 'staff__course').order_by('id'))
    context = {
        'allStaff': allStaff,
        'page_obj': allStaff,
        'page_title': 'Manage Staff'
    }
    return render(request, "hod_template/manage_staff.html", context)


def manage_student(request):
    students = paginate(request, CustomUser.objects.filter(user_type=3).select_related('student', 'student__course', 'student__session').order_by('id'))
    context = {
        'students': students,
        'page_obj': students,
        'page_title': 'Manage Students'
    }
    return render(request, "hod_template/manage_student.html", context)


def manage_course(request):
    courses = paginate(request, Course.objects.order_by('id'))
    context = {
        'courses': courses,
        'page_obj': courses,
        'page_title': 'Manage Courses'
    }
    return render(request, "hod_template/manage_course.html", context)


def manage_subject(request):
    subjects = paginate(request, Subject.objects.order_by('id'))
    context = {
        'subjects': subjects,
        'page_obj': subjects,
        'page_title': 'Manage Subjects'
    }
    return render(request, "hod_template/manage_subject.html", context)


def edit_staff(request, staff_id):
    staff = get_object_or_404(Staff, id=staff_id)
    form = StaffForm(request.POST or None, instance=staff)
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
            course = form.cleaned_data.get('course')
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
                staff.course = course
                user.save()
                staff.save()
                log_action(request, 'updated', 'Staff', user)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_staff', args=[staff_id]))
            except Exception as e:
                logger.exception('Unhandled error in edit_staff')
                messages.error(request, "Could Not Update " + str(e))
        else:
            messages.error(request, "Please fill the form properly")
    else:
        return render(request, "hod_template/edit_staff_template.html", context)


def edit_student(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    form = StudentForm(request.POST or None, instance=student)
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
                messages.error(request, "Could Not Update " + str(e))
        else:
            messages.error(request, "Please Fill Form Properly!")
    else:
        return render(request, "hod_template/edit_student_template.html", context)


def edit_course(request, course_id):
    instance = get_object_or_404(Course, id=course_id)
    form = CourseForm(request.POST or None, instance=instance)
    context = {
        'form': form,
        'course_id': course_id,
        'page_title': 'Edit Course'
    }
    if request.method == 'POST':
        if form.is_valid():
            name = form.cleaned_data.get('name')
            try:
                course = Course.objects.get(id=course_id)
                course.name = name
                course.save()
                log_action(request, 'updated', 'Course', course)
                messages.success(request, "Successfully Updated")
            except:
                logger.exception('Unhandled error in edit_course')
                messages.error(request, "Could Not Update")
        else:
            messages.error(request, "Could Not Update")

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
                messages.error(request, "Could Not Add " + str(e))
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
                messages.error(request, 'Could Not Add ' + str(e))
        else:
            messages.error(request, 'Fill Form Properly ')
    return render(request, "hod_template/add_session_template.html", context)


def manage_session(request):
    sessions = paginate(request, Session.objects.order_by('id'))
    context = {'sessions': sessions, 'page_obj': sessions, 'page_title': 'Manage Sessions'}
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
        feedbacks = paginate(request, FeedbackStaff.objects.order_by('id'))
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
        allLeave = paginate(request, LeaveReportStaff.objects.order_by('id'))
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
            leave.status = status
            leave.save()
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
            leave.status = status
            leave.save()
            return HttpResponse(True)
        except Exception as e:
            logger.exception("Failed to update student leave status")
            return HttpResponse(False)


def admin_view_attendance(request):
    subjects = Subject.objects.all()
    sessions = Session.objects.all()
    context = {
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
        attendance_reports = AttendanceReport.objects.filter(
            attendance=attendance)
        json_data = []
        for report in attendance_reports:
            data = {
                "status":  str(report.status),
                "name": str(report.student)
            }
            json_data.append(data)
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
                messages.error(request, "Invalid Data Provided")
        except Exception as e:
            logger.exception('Unhandled error in admin_view_profile')
            messages.error(
                request, "Error Occurred While Updating Profile " + str(e))
    return render(request, "hod_template/admin_view_profile.html", context)


def admin_notify_staff(request):
    staff = CustomUser.objects.filter(user_type=2)
    context = {
        'page_title': "Send Notifications To Staff",
        'allStaff': staff
    }
    return render(request, "hod_template/staff_notification.html", context)


def admin_notify_student(request):
    student = CustomUser.objects.filter(user_type=3)
    context = {
        'page_title': "Send Notifications To Students",
        'students': student
    }
    return render(request, "hod_template/student_notification.html", context)


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


def delete_staff(request, staff_id):
    staff = get_object_or_404(CustomUser, staff__id=staff_id)
    try:
        staff_repr = str(staff)
        staff.delete()
        log_action(request, 'deleted', 'Staff', staff_repr)
        messages.success(request, "Staff deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_staff')
        messages.error(request, "Could not delete this staff member.")
    return redirect(reverse('manage_staff'))


def delete_student(request, student_id):
    student = get_object_or_404(CustomUser, student__id=student_id)
    try:
        student_repr = str(student)
        student.delete()
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
        log_action(request, 'deleted', 'Course', course_repr)
        messages.success(request, "Course deleted successfully!")
    except Exception:
        logger.exception('Unhandled error in delete_course')
        messages.error(
            request, "Sorry, some students are assigned to this course already. Kindly change the affected student course and try again")
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

def _attendance_summary_data(request):
    course_id = request.GET.get('course') or ''
    session_id = request.GET.get('session') or ''
    start_date = request.GET.get('start_date') or date.today().replace(day=1).isoformat()
    end_date = request.GET.get('end_date') or date.today().isoformat()

    reports = AttendanceReport.objects.filter(
        attendance__date__gte=start_date, attendance__date__lte=end_date)
    if course_id:
        reports = reports.filter(attendance__subject__course_id=course_id)
    if session_id:
        reports = reports.filter(attendance__session_id=session_id)

    def with_percent(rows, key):
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
    by_student_raw = (
        reports.values('student_id', 'student__admin__first_name', 'student__admin__last_name')
        .annotate(total=Count('id'), present=Count('id', filter=Q(status=True)))
    )
    student_summary = []
    for row in by_student_raw:
        total = row['total']
        present = row['present']
        percent = round(present / total * 100, 1) if total else 0
        student_summary.append({
            'name': f"{row['student__admin__last_name']}, {row['student__admin__first_name']}",
            'total': total,
            'present': present,
            'absent': total - present,
            'percent': percent,
            'flagged': percent < CHRONIC_ABSENCE_THRESHOLD,
        })
    # Worst attendance first, so chronic absences surface immediately.
    student_summary.sort(key=lambda r: r['percent'])

    return {
        'course_id': course_id,
        'session_id': session_id,
        'start_date': start_date,
        'end_date': end_date,
        'course_summary': with_percent(by_course, 'course'),
        'daily_summary': with_percent(by_day, 'date'),
        'student_summary': student_summary,
    }


def report_attendance_summary(request):
    data = _attendance_summary_data(request)
    context = {
        'page_title': 'Attendance Summary Report',
        'courses': Course.objects.order_by('name'),
        'sessions': Session.objects.order_by('-start_year'),
        'selected_course': data['course_id'],
        'selected_session': data['session_id'],
        'start_date': data['start_date'],
        'end_date': data['end_date'],
        'course_summary': data['course_summary'],
        'daily_summary': data['daily_summary'],
        'student_summary': data['student_summary'],
        'chronic_threshold': CHRONIC_ABSENCE_THRESHOLD,
    }
    return render(request, 'hod_template/report_attendance_summary.html', context)


def report_attendance_summary_csv(request):
    data = _attendance_summary_data(request)
    rows = [(row['course'], row['total'], row['present'], row['absent'], f"{row['percent']}%")
            for row in data['course_summary']]
    filename = f"attendance_summary_{data['start_date']}_to_{data['end_date']}.csv"
    return csv_response(filename, ['Course', 'Total', 'Present', 'Absent', 'Attendance %'], rows)


def report_attendance_by_student_csv(request):
    data = _attendance_summary_data(request)
    rows = [(row['name'], row['total'], row['present'], row['absent'], f"{row['percent']}%",
             'Yes' if row['flagged'] else '') for row in data['student_summary']]
    filename = f"attendance_by_student_{data['start_date']}_to_{data['end_date']}.csv"
    return csv_response(filename, ['Student', 'Total', 'Present', 'Absent', 'Attendance %', 'Below Threshold'], rows)


def report_student_attendance(request):
    student_id = request.GET.get('student') or ''
    start_date = request.GET.get('start_date') or ''
    end_date = request.GET.get('end_date') or ''

    records = []
    summary = None
    if student_id:
        student = get_object_or_404(Student, id=student_id)
        reports = AttendanceReport.objects.filter(student=student).select_related(
            'attendance', 'attendance__subject')
        if start_date:
            reports = reports.filter(attendance__date__gte=start_date)
        if end_date:
            reports = reports.filter(attendance__date__lte=end_date)
        reports = reports.order_by('-attendance__date')
        total = reports.count()
        present = reports.filter(status=True).count()
        summary = {
            'student': student,
            'total': total,
            'present': present,
            'absent': total - present,
            'percent': round(present / total * 100, 1) if total else 0,
        }
        records = reports

    context = {
        'page_title': 'Student Attendance History',
        'students': Student.objects.select_related('admin').order_by('admin__first_name'),
        'selected_student': student_id,
        'start_date': start_date,
        'end_date': end_date,
        'records': records,
        'summary': summary,
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
    start_date = request.GET.get('start_date') or ''
    end_date = request.GET.get('end_date') or ''

    status_map = {'pending': 0, 'approved': 1, 'rejected': -1}

    student_leaves = LeaveReportStudent.objects.select_related('student', 'student__admin')
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

    records = []
    if leave_type in ('all', 'student'):
        records += [{
            'type': 'Student', 'name': str(l.student), 'date': l.date,
            'message': l.message, 'status_label': LEAVE_STATUS_LABELS.get(l.status, 'Unknown'),
        } for l in student_leaves]
    if leave_type in ('all', 'staff'):
        records += [{
            'type': 'Staff', 'name': str(l.staff), 'date': l.date,
            'message': l.message, 'status_label': LEAVE_STATUS_LABELS.get(l.status, 'Unknown'),
        } for l in staff_leaves]
    records.sort(key=lambda r: r['date'], reverse=True)

    return {
        'leave_type': leave_type, 'status': status,
        'start_date': start_date, 'end_date': end_date,
        'records': records,
    }


def report_leave(request):
    data = _leave_report_data(request)
    context = {
        'page_title': 'Leave Report',
        'records': data['records'],
        'selected_type': data['leave_type'],
        'selected_status': data['status'],
        'start_date': data['start_date'],
        'end_date': data['end_date'],
    }
    return render(request, 'hod_template/report_leave.html', context)


def report_leave_csv(request):
    data = _leave_report_data(request)
    rows = [(r['type'], r['name'], r['date'], r['status_label'], r['message']) for r in data['records']]
    return csv_response('leave_report.csv', ['Type', 'Name', 'Date', 'Status', 'Message'], rows)


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
    return csv_response('results_report.csv', ['Course', 'Subject', 'Student', 'Test', 'Exam'], rows)


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
