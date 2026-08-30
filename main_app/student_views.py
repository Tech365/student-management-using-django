import json
import logging
import math
from datetime import datetime

from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import (FeedbackStudentForm, LeaveReportStudentForm,
                    StudentEditForm)
from .models import (Attendance, AttendanceReport, Course, CustomUser,
                     FeedbackStudent, LeaveReportStudent, NotificationStaff,
                     NotificationStudent, Staff, Student, StudentResult,
                     Subject)
from .utils import attendance_with_leave_json, send_notification_email

logger = logging.getLogger(__name__)


def student_home(request):
    student = get_object_or_404(Student, admin=request.user)
    total_subject = Subject.objects.filter(course=student.course).count()
    total_attendance = AttendanceReport.objects.filter(student=student).count()
    total_present = AttendanceReport.objects.filter(student=student, status=True).count()
    if total_attendance == 0:  # Don't divide. DivisionByZero
        percent_absent = percent_present = 0
    else:
        percent_present = math.floor((total_present/total_attendance) * 100)
        percent_absent = math.ceil(100 - percent_present)
    subjects = Subject.objects.filter(course=student.course)
    counts_by_subject = {
        row['attendance__subject']: row
        for row in AttendanceReport.objects.filter(student=student, attendance__subject__in=subjects)
        .values('attendance__subject')
        .annotate(present=Count('id', filter=Q(status=True)), absent=Count('id', filter=Q(status=False)))
    }
    subject_name = [subject.name for subject in subjects]
    data_present = [counts_by_subject.get(subject.id, {}).get('present', 0) for subject in subjects]
    data_absent = [counts_by_subject.get(subject.id, {}).get('absent', 0) for subject in subjects]
    context = {
        'total_attendance': total_attendance,
        'percent_present': percent_present,
        'percent_absent': percent_absent,
        'total_subject': total_subject,
        'subjects': subjects,
        'data_present': data_present,
        'data_absent': data_absent,
        'data_name': subject_name,
        'page_title': 'Student Homepage'

    }
    return render(request, 'student_template/home_content.html', context)


def student_view_attendance(request):
    student = get_object_or_404(Student, admin=request.user)
    if request.method != 'POST':
        subjects = Subject.objects.filter(course=student.course) if student.course_id else Subject.objects.none()
        context = {
            'subjects': subjects,
            'page_title': 'View Attendance'
        }
        return render(request, 'student_template/student_view_attendance.html', context)
    else:
        subject_id = request.POST.get('subject')
        start = request.POST.get('start_date')
        end = request.POST.get('end_date')
        try:
            # Scoped to the student's own class - matches the scoping
            # convention used everywhere else (see staff_views.py). Not
            # currently reachable another way since attendance_with_leave_json
            # always re-filters by this exact student, but that shouldn't be
            # the only thing standing between this and another class's data.
            subject = get_object_or_404(Subject, id=subject_id, course=student.course)
            start_date = datetime.strptime(start, "%Y-%m-%d")
            end_date = datetime.strptime(end, "%Y-%m-%d")
            attendance = Attendance.objects.filter(
                date__range=(start_date, end_date), subject=subject)
            json_data = attendance_with_leave_json(student, attendance)
            return JsonResponse(json.dumps(json_data), safe=False)
        except Exception:
            logger.exception("Failed to fetch student attendance")
            return JsonResponse({'error': 'Could not fetch attendance.'}, status=400)


def student_apply_leave(request):
    form = LeaveReportStudentForm(request.POST or None)
    student = get_object_or_404(Student, admin_id=request.user.id)
    context = {
        'form': form,
        'leave_history': LeaveReportStudent.objects.filter(student=student),
        'page_title': 'Apply for leave'
    }
    if request.method == 'POST':
        if form.is_valid():
            # One application can cover several dates at once - each is
            # its own LeaveReportStudent row (the model represents one
            # date per record), created independently so one bad date
            # can't block the rest, mirroring the per-row isolation the
            # CSV bulk-upload views already use.
            message = form.cleaned_data['message']
            # Notify every teacher of this class, i.e. every staff who
            # teaches at least one subject in it - a class can have more
            # than one teacher. Subject.course is never null, so
            # student.course=None naturally matches no teachers here.
            teachers = Staff.objects.filter(subject__course=student.course).distinct()
            created = []
            skipped = []
            for d in form.cleaned_data['dates']:
                if LeaveReportStudent.objects.filter(student=student, date=d).exists():
                    skipped.append(d)
                    continue
                try:
                    LeaveReportStudent.objects.create(student=student, date=d, message=message)
                    created.append(d)
                except Exception:
                    logger.exception('Failed to create leave for %s on %s', student, d)
                    skipped.append(d)
                    continue
                notif_message = f"{student} applied for leave on {d}: {message}"
                for teacher in teachers:
                    NotificationStaff.objects.create(staff=teacher, message=notif_message)
                    send_notification_email(teacher.admin, notif_message)
            return JsonResponse({'created': created, 'skipped': skipped})
        return JsonResponse({'errors': form.errors}, status=400)
    return render(request, "student_template/student_apply_leave.html", context)


def student_feedback(request):
    form = FeedbackStudentForm(request.POST or None)
    student = get_object_or_404(Student, admin_id=request.user.id)
    context = {
        'form': form,
        'feedbacks': FeedbackStudent.objects.filter(student=student),
        'page_title': 'Student Feedback'

    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.student = student
                obj.save()
                messages.success(
                    request, "Feedback submitted for review")
                return redirect(reverse('student_feedback'))
            except Exception:
                logger.exception('Unhandled error in student_feedback')
                messages.error(request, "Could not Submit!")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "student_template/student_feedback.html", context)


def student_view_profile(request):
    student = get_object_or_404(Student, admin=request.user)
    form = StudentEditForm(request.POST or None, request.FILES or None,
                           instance=student)
    context = {'form': form,
               'page_title': 'View/Edit Profile'
               }
    if request.method == 'POST':
        try:
            if form.is_valid():
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                password = form.cleaned_data.get('password') or None
                address = form.cleaned_data.get('address')
                gender = form.cleaned_data.get('gender')
                passport = request.FILES.get('profile_pic') or None
                admin = student.admin
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
                student.save()
                messages.success(request, "Profile Updated!")
                return redirect(reverse('student_view_profile'))
            else:
                messages.error(request, "Please check the form - some required fields are missing or invalid.")
        except Exception as e:
            logger.exception('Unhandled error in student_view_profile')
            messages.error(request, "Error Occurred While Updating Profile " + str(e))

    return render(request, "student_template/student_view_profile.html", context)


def student_fcmtoken(request):
    token = request.POST.get('token')
    student_user = get_object_or_404(CustomUser, id=request.user.id)
    try:
        student_user.fcm_token = token
        student_user.save()
        return HttpResponse("True")
    except Exception as e:
        logger.exception('Unhandled error in student_fcmtoken')
        return HttpResponse("False")


def student_view_notification(request):
    student = get_object_or_404(Student, admin=request.user)
    notifications = list(NotificationStudent.objects.filter(student=student))
    context = {
        'notifications': notifications,
        'page_title': "View Notifications"
    }
    NotificationStudent.objects.filter(student=student, is_read=False).update(is_read=True)
    return render(request, "student_template/student_view_notification.html", context)


def delete_student_notification(request, notification_id):
    student = get_object_or_404(Student, admin=request.user)
    try:
        # Scoped to `student` so a student can't delete another student's
        # notification by guessing an id.
        notification = get_object_or_404(NotificationStudent, id=notification_id, student=student)
        notification.delete()
        return HttpResponse(True)
    except Exception:
        logger.exception("Failed to delete student notification")
        return HttpResponse(False)


def student_view_result(request):
    student = get_object_or_404(Student, admin=request.user)
    results = StudentResult.objects.filter(student=student)
    context = {
        'results': results,
        'page_title': "View Results"
    }
    return render(request, "student_template/student_view_result.html", context)
