import json
import logging

from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import FeedbackStaffForm, LeaveReportStaffForm, StaffEditForm
from .models import (Attendance, AttendanceReport, Course, CustomUser,
                     FeedbackStaff, LeaveReportStaff, LeaveReportStudent,
                     NotificationStaff, NotificationStudent, Session, Staff,
                     Student, StudentResult, Subject)
from .utils import (leave_decision_message, paginate, send_notification_email,
                    teacher_course_ids)

logger = logging.getLogger(__name__)


def staff_home(request):
    staff = get_object_or_404(Staff, admin=request.user)
    total_students = Student.objects.filter(course=staff.course).count()
    total_leave = LeaveReportStaff.objects.filter(staff=staff).count()
    subjects = Subject.objects.filter(staff=staff)
    total_subject = subjects.count()
    total_attendance = Attendance.objects.filter(subject__in=subjects).count()
    attendance_counts = dict(
        Attendance.objects.filter(subject__in=subjects)
        .values_list('subject').annotate(count=Count('id'))
    )
    subject_list = [subject.name for subject in subjects]
    attendance_list = [attendance_counts.get(subject.id, 0) for subject in subjects]
    context = {
        'page_title': 'Staff Panel - ' + str(staff.admin.last_name) + ' (' + str(staff.course) + ')',
        'total_students': total_students,
        'total_attendance': total_attendance,
        'total_leave': total_leave,
        'total_subject': total_subject,
        'subject_list': subject_list,
        'attendance_list': attendance_list
    }
    return render(request, 'staff_template/home_content.html', context)


def staff_take_attendance(request):
    staff = get_object_or_404(Staff, admin=request.user)
    subjects = Subject.objects.filter(staff_id=staff).select_related('course')
    courses = Course.objects.filter(id__in=subjects.values_list('course_id', flat=True)).order_by('name')
    sessions = Session.objects.all()
    context = {
        'subjects': subjects,
        'courses': courses,
        'sessions': sessions,
        'page_title': 'Take Attendance'
    }

    return render(request, 'staff_template/staff_take_attendance.html', context)


def _approved_leave_student_ids(students, attendance_date):
    """IDs of `students` with an approved leave for `attendance_date`.
    LeaveReportStudent.date is a free-text field fed by the same HTML5
    date input as attendance_date, so a plain string match is reliable."""
    if not attendance_date:
        return set()
    return set(
        LeaveReportStudent.objects.filter(
            student__in=students, date=attendance_date, status=1
        ).values_list('student_id', flat=True)
    )


def get_students(request):
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    attendance_date = request.POST.get('date')
    try:
        subject = get_object_or_404(Subject, id=subject_id)
        session = get_object_or_404(Session, id=session_id)
        students = Student.objects.filter(
            course_id=subject.course.id, session=session)
        on_leave_ids = _approved_leave_student_ids(students, attendance_date)
        student_data = []
        for student in students:
            data = {
                    "id": student.id,
                    "name": student.admin.last_name + " " + student.admin.first_name,
                    "on_leave": student.id in on_leave_ids,
                    }
            student_data.append(data)
        return JsonResponse(json.dumps(student_data), content_type='application/json', safe=False)
    except Exception as e:
        logger.exception("Failed to fetch students")
        return JsonResponse({'error': str(e)}, status=400)


def save_attendance(request):
    student_data = request.POST.get('student_ids')
    date = request.POST.get('date')
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    students = json.loads(student_data)
    try:
        session = get_object_or_404(Session, id=session_id)
        subject = get_object_or_404(Subject, id=subject_id)

        # Check if an attendance object already exists for the given date and session
        attendance, created = Attendance.objects.get_or_create(session=session, subject=subject, date=date)

        submitted_ids = [student_dict.get('id') for student_dict in students]
        on_leave_ids = _approved_leave_student_ids(
            Student.objects.filter(id__in=submitted_ids), date)

        for student_dict in students:
            student = get_object_or_404(Student, id=student_dict.get('id'))
            if student.id in on_leave_ids:
                # Approved leave for this date - don't record attendance
                # for them at all, even if the client tried to send one.
                continue

            # get_or_create so re-taking attendance for the same
            # student/date (e.g. correcting a mistake) updates the
            # existing report instead of creating a duplicate.
            attendance_report, _ = AttendanceReport.objects.get_or_create(student=student, attendance=attendance)
            attendance_report.status = student_dict.get('status')
            attendance_report.save()

    except Exception as e:
        logger.exception("Failed to save attendance")
        return HttpResponse("False")

    return HttpResponse("OK")


def staff_update_attendance(request):
    staff = get_object_or_404(Staff, admin=request.user)
    subjects = Subject.objects.filter(staff_id=staff)
    courses = Course.objects.filter(id__in=subjects.values_list('course_id', flat=True)).order_by('name')
    sessions = Session.objects.all()
    context = {
        'subjects': subjects,
        'courses': courses,
        'sessions': sessions,
        'page_title': 'Update Attendance'
    }

    return render(request, 'staff_template/staff_update_attendance.html', context)


def get_student_attendance(request):
    attendance_date_id = request.POST.get('attendance_date_id')
    try:
        attendance = get_object_or_404(Attendance, id=attendance_date_id)
        # The full class roster, not just students with an existing
        # AttendanceReport - a student on approved leave never gets one
        # (see save_attendance), but should still show up here, disabled,
        # rather than silently disappearing from the list.
        students = Student.objects.filter(
            course_id=attendance.subject.course_id, session=attendance.session)
        reports_by_student = {
            r.student_id: r for r in AttendanceReport.objects.filter(attendance=attendance)
        }
        on_leave_ids = _approved_leave_student_ids(students, attendance.date.isoformat())
        student_data = []
        for student in students:
            report = reports_by_student.get(student.id)
            data = {
                "id": student.admin.id,
                "name": student.admin.last_name + " " + student.admin.first_name,
                "status": report.status if report else None,
                "on_leave": student.id in on_leave_ids,
            }
            student_data.append(data)
        return JsonResponse(json.dumps(student_data), content_type='application/json', safe=False)
    except Exception as e:
        logger.exception("Failed to fetch student attendance")
        return JsonResponse({'error': str(e)}, status=400)


def update_attendance(request):
    student_data = request.POST.get('student_ids')
    date = request.POST.get('date')
    students = json.loads(student_data)
    try:
        attendance = get_object_or_404(Attendance, id=date)

        admin_ids = [student_dict.get('id') for student_dict in students]
        students_by_admin_id = {
            s.admin_id: s for s in Student.objects.filter(admin_id__in=admin_ids)
        }
        on_leave_ids = _approved_leave_student_ids(
            students_by_admin_id.values(), attendance.date.isoformat())

        for student_dict in students:
            student = students_by_admin_id.get(student_dict.get('id')) \
                or get_object_or_404(Student, admin_id=student_dict.get('id'))
            if student.id in on_leave_ids:
                # Approved leave for this date - don't record attendance
                # for them at all, even if the client tried to send one.
                continue
            attendance_report = get_object_or_404(AttendanceReport, student=student, attendance=attendance)
            attendance_report.status = student_dict.get('status')
            attendance_report.save()
    except Exception as e:
        logger.exception("Failed to update attendance")
        return HttpResponse("False")

    return HttpResponse("OK")


def staff_apply_leave(request):
    form = LeaveReportStaffForm(request.POST or None)
    staff = get_object_or_404(Staff, admin_id=request.user.id)
    context = {
        'form': form,
        'leave_history': LeaveReportStaff.objects.filter(staff=staff),
        'page_title': 'Apply for Leave'
    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.staff = staff
                obj.save()
                message = f"{staff} applied for leave on {obj.date}: {obj.message}"
                # Admin has no in-app notification inbox, so email only.
                for admin_user in CustomUser.objects.filter(user_type=1):
                    send_notification_email(admin_user, message)
                messages.success(
                    request, "Application for leave has been submitted for review")
                return redirect(reverse('staff_apply_leave'))
            except Exception:
                logger.exception('Unhandled error in staff_apply_leave')
                messages.error(request, "Could not apply!")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "staff_template/staff_apply_leave.html", context)


def staff_view_student_leave(request):
    staff = get_object_or_404(Staff, admin=request.user)
    # A class can have more than one teacher, and a teacher can teach in
    # more than one class - "their class" is every course they teach at
    # least one subject in, not just Staff.course (a single "home" class).
    # An empty set here correctly matches nothing via __in, unlike
    # comparing a nullable FK directly.
    taught_course_ids = teacher_course_ids(staff)
    if request.method != 'POST':
        allLeave = paginate(
            request,
            LeaveReportStudent.objects.filter(student__course_id__in=taught_course_ids).order_by('-id')
        )
        context = {
            'allLeave': allLeave,
            'page_obj': allLeave,
            'page_title': 'Student Leave Requests'
        }
        return render(request, "staff_template/student_leave_view.html", context)
    else:
        id = request.POST.get('id')
        status = request.POST.get('status')
        status = 1 if status == '1' else -1
        try:
            # Restrict to leave requests from a class the staff member
            # actually teaches, so a teacher can't approve/reject another
            # class's leave by id.
            leave = get_object_or_404(LeaveReportStudent, id=id, student__course_id__in=taught_course_ids)
            if leave.status != 0:
                # Already decided (e.g. by admin, or a duplicate submit) -
                # don't overwrite the decision or send a second notification.
                return HttpResponse(False)
            leave.status = status
            leave.save()
            message = leave_decision_message(leave.date, status)
            NotificationStudent.objects.create(student=leave.student, message=message)
            send_notification_email(leave.student.admin, message)
            return HttpResponse(True)
        except Exception:
            logger.exception("Failed to update student leave status")
            return HttpResponse(False)


def staff_feedback(request):
    form = FeedbackStaffForm(request.POST or None)
    staff = get_object_or_404(Staff, admin_id=request.user.id)
    context = {
        'form': form,
        'feedbacks': FeedbackStaff.objects.filter(staff=staff),
        'page_title': 'Add Feedback'
    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.staff = staff
                obj.save()
                messages.success(request, "Feedback submitted for review")
                return redirect(reverse('staff_feedback'))
            except Exception:
                logger.exception('Unhandled error in staff_feedback')
                messages.error(request, "Could not Submit!")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "staff_template/staff_feedback.html", context)


def staff_view_profile(request):
    staff = get_object_or_404(Staff, admin=request.user)
    form = StaffEditForm(request.POST or None, request.FILES or None,instance=staff)
    context = {'form': form, 'page_title': 'View/Update Profile'}
    if request.method == 'POST':
        try:
            if form.is_valid():
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                password = form.cleaned_data.get('password') or None
                address = form.cleaned_data.get('address')
                gender = form.cleaned_data.get('gender')
                passport = request.FILES.get('profile_pic') or None
                admin = staff.admin
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
                staff.save()
                messages.success(request, "Profile Updated!")
                return redirect(reverse('staff_view_profile'))
            else:
                messages.error(request, "Invalid Data Provided")
                return render(request, "staff_template/staff_view_profile.html", context)
        except Exception as e:
            logger.exception('Unhandled error in staff_view_profile')
            messages.error(
                request, "Error Occurred While Updating Profile " + str(e))
            return render(request, "staff_template/staff_view_profile.html", context)

    return render(request, "staff_template/staff_view_profile.html", context)


def staff_fcmtoken(request):
    token = request.POST.get('token')
    try:
        staff_user = get_object_or_404(CustomUser, id=request.user.id)
        staff_user.fcm_token = token
        staff_user.save()
        return HttpResponse("True")
    except Exception as e:
        logger.exception('Unhandled error in staff_fcmtoken')
        return HttpResponse("False")


def staff_view_notification(request):
    staff = get_object_or_404(Staff, admin=request.user)
    notifications = list(NotificationStaff.objects.filter(staff=staff))
    context = {
        'notifications': notifications,
        'page_title': "View Notifications"
    }
    NotificationStaff.objects.filter(staff=staff, is_read=False).update(is_read=True)
    return render(request, "staff_template/staff_view_notification.html", context)


def staff_add_result(request):
    staff = get_object_or_404(Staff, admin=request.user)
    subjects = Subject.objects.filter(staff=staff)
    sessions = Session.objects.all()
    context = {
        'page_title': 'Result Upload',
        'subjects': subjects,
        'sessions': sessions
    }
    if request.method == 'POST':
        try:
            student_id = request.POST.get('student_list')
            subject_id = request.POST.get('subject')
            test = request.POST.get('test')
            exam = request.POST.get('exam')
            student = get_object_or_404(Student, id=student_id)
            # Scoped to `staff` so a staff member can't submit another
            # teacher's subject_id and edit grades they don't own.
            subject = get_object_or_404(Subject, id=subject_id, staff=staff)
            try:
                data = StudentResult.objects.get(
                    student=student, subject=subject)
                data.exam = exam
                data.test = test
                data.save()
                messages.success(request, "Scores Updated")
            except:
                logger.exception('Unhandled error in staff_add_result')
                result = StudentResult(student=student, subject=subject, test=test, exam=exam)
                result.save()
                messages.success(request, "Scores Saved")
        except Exception as e:
            logger.exception('Unhandled error in staff_add_result')
            messages.warning(request, "Error Occurred While Processing Form")
    return render(request, "staff_template/staff_add_result.html", context)


def fetch_student_result(request):
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        subject_id = request.POST.get('subject')
        student_id = request.POST.get('student')
        student = get_object_or_404(Student, id=student_id)
        # Scoped to `staff` so a staff member can't read another
        # teacher's subject results by guessing subject_id.
        subject = get_object_or_404(Subject, id=subject_id, staff=staff)
        result = StudentResult.objects.get(student=student, subject=subject)
        result_data = {
            'exam': result.exam,
            'test': result.test
        }
        return HttpResponse(json.dumps(result_data))
    except Exception as e:
        logger.exception('Unhandled error in fetch_student_result')
        return HttpResponse('False')
