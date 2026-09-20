import json
import logging
from datetime import date, datetime

from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import FeedbackStaffForm, LeaveReportStaffForm, StaffEditForm
from .models import (Attendance, AttendanceReport, Course, CustomUser,
                     FeedbackStaff, LeaveReportStaff, LeaveReportStudent,
                     NotificationStaff, Session, Staff,
                     Student, StudentResult, Subject)
from .utils import (all_configured_school_weekdays, approved_leave_student_ids,
                    attendance_not_taken_rows, build_take_attendance_roster,
                    notify_student_leave_decision, paginate,
                    resolve_attendance_subject, save_take_attendance,
                    send_notification_email, session_course_ids_map,
                    take_attendance_date_error, teacher_course_ids)

logger = logging.getLogger(__name__)


def staff_home(request):
    staff = get_object_or_404(Staff, admin=request.user)
    subjects = Subject.objects.filter(staff=staff).select_related('course')
    # A teacher's classes are every course they teach at least one
    # subject in - not a single "home class" - so list them all rather
    # than picking one.
    class_names = sorted({subject.course.name for subject in subjects})
    taught_course_ids = teacher_course_ids(staff)
    total_students = Student.objects.filter(course_id__in=taught_course_ids).count()
    total_leave = LeaveReportStaff.objects.filter(staff=staff).count()
    total_subject = subjects.count()
    total_attendance = Attendance.objects.filter(subject__in=subjects).count()
    attendance_counts = dict(
        Attendance.objects.filter(subject__in=subjects)
        .values_list('subject').annotate(count=Count('id'))
    )
    subject_list = [subject.name for subject in subjects]
    attendance_list = [attendance_counts.get(subject.id, 0) for subject in subjects]
    class_label = ', '.join(class_names) if class_names else 'No class assigned'
    context = {
        'page_title': 'Staff Panel - ' + str(staff.admin.last_name) + ' (' + class_label + ')',
        'total_students': total_students,
        'total_attendance': total_attendance,
        'total_leave': total_leave,
        'total_subject': total_subject,
        'subject_list': subject_list,
        'attendance_list': attendance_list
    }
    return render(request, 'staff_template/home_content.html', context)


def _staff_class_context(staff):
    """Class/Subject/Session dropdown data shared by the Take/View/Update
    Attendance screens - each renders the same three cascading selects,
    just wired to different fetch endpoints."""
    subjects = Subject.objects.filter(staff=staff).select_related('course')
    courses = Course.objects.filter(id__in=subjects.values_list('course_id', flat=True)).order_by('name')
    sessions = Session.objects.all()
    session_courses = session_course_ids_map()
    for session in sessions:
        session.course_ids_str = ' '.join(str(c) for c in session_courses.get(session.id, []))
    return {'subjects': subjects, 'courses': courses, 'sessions': sessions}


def staff_take_attendance(request):
    staff = get_object_or_404(Staff, admin=request.user)
    context = _staff_class_context(staff)
    context['page_title'] = 'Take Attendance'
    return render(request, 'staff_template/staff_take_attendance.html', context)


def staff_view_attendance(request):
    staff = get_object_or_404(Staff, admin=request.user)
    context = _staff_class_context(staff)
    context['page_title'] = 'View Attendance'
    return render(request, 'staff_template/staff_view_attendance.html', context)


def get_students(request):
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    attendance_date = request.POST.get('date')
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        # Scoped to `staff` so a teacher can't submit another teacher's
        # subject_id and pull up a class they don't teach. Still works
        # unchanged now that staff is many-to-many - Django treats
        # staff=<instance> as an "is one of this subject's teachers" test.
        subject = resolve_attendance_subject(subject_id, staff=staff)
        session = get_object_or_404(Session, id=session_id)

        date_obj = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        date_error = take_attendance_date_error(session, date_obj)
        if date_error:
            return JsonResponse({'error': date_error}, status=400)

        payload = build_take_attendance_roster(subject, session, attendance_date)
        return JsonResponse(json.dumps(payload), content_type='application/json', safe=False)
    except Exception:
        logger.exception("Failed to fetch students")
        return JsonResponse({'error': 'Could not fetch students.'}, status=400)


def save_attendance(request):
    student_data = request.POST.get('student_ids')
    date = request.POST.get('date')
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    students = json.loads(student_data)
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        session = get_object_or_404(Session, id=session_id)
        # Scoped to `staff` so a teacher can't record/overwrite attendance
        # for a class they don't teach by submitting another subject_id.
        subject = resolve_attendance_subject(subject_id, staff=staff)

        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
        if take_attendance_date_error(session, date_obj):
            return HttpResponse("False")

        save_take_attendance(subject, session, date, students, taken_by=staff)
    except Exception as e:
        logger.exception("Failed to save attendance")
        return HttpResponse("False")

    return HttpResponse("OK")


def staff_update_attendance(request):
    staff = get_object_or_404(Staff, admin=request.user)
    context = _staff_class_context(staff)
    context['page_title'] = 'Update Attendance'
    return render(request, 'staff_template/staff_update_attendance.html', context)


def get_student_attendance(request):
    attendance_date_id = request.POST.get('attendance_date_id')
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        # Scoped to `staff` so a teacher can't view another class's
        # attendance by submitting another attendance_date_id.
        attendance = get_object_or_404(Attendance, id=attendance_date_id, subject__staff=staff)
        # The full class roster, not just students with an existing
        # AttendanceReport - a student on approved leave never gets one
        # (see save_attendance), but should still show up here, disabled,
        # rather than silently disappearing from the list.
        students = Student.objects.filter(
            Q(course_id=attendance.subject.course_id) | Q(secondary_courses=attendance.subject.course_id),
            session=attendance.session, admin__is_active=True,
        ).distinct()
        reports_by_student = {
            r.student_id: r for r in AttendanceReport.objects.filter(attendance=attendance)
        }
        on_leave_ids = approved_leave_student_ids(students, attendance.date.isoformat())
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
    except Exception:
        logger.exception("Failed to fetch student attendance")
        return JsonResponse({'error': 'Could not fetch attendance.'}, status=400)


def update_attendance(request):
    student_data = request.POST.get('student_ids')
    date = request.POST.get('date')
    students = json.loads(student_data)
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        # Scoped to `staff` so a teacher can't overwrite another class's
        # attendance by submitting another attendance id.
        attendance = get_object_or_404(Attendance, id=date, subject__staff=staff)
        attendance.taken_by = staff
        attendance.save()

        admin_ids = [student_dict.get('id') for student_dict in students]
        # Scoped to the attendance's own class, same reasoning as save_attendance.
        students_by_admin_id = {
            s.admin_id: s for s in Student.objects.filter(
                admin_id__in=admin_ids, course_id=attendance.subject.course_id)
        }
        on_leave_ids = approved_leave_student_ids(
            students_by_admin_id.values(), attendance.date.isoformat())

        for student_dict in students:
            student = students_by_admin_id.get(student_dict.get('id'))
            if student is None:
                # Not in this class - don't fall back to an unscoped
                # lookup, or a submitted id from another course could be
                # used to poke at this class's attendance records.
                continue
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


def staff_report_attendance_not_taken(request):
    """Teacher-scoped version of the admin's Attendance Not Taken report -
    only this teacher's own subjects (co-teaching aware, via Subject.staff),
    so they can quickly see what they've missed instead of scrolling
    through the whole school's list."""
    staff = get_object_or_404(Staff, admin=request.user)
    selected_date = request.GET.get('date') or date.today().isoformat()
    course_id = request.GET.get('course') or ''
    not_taken = attendance_not_taken_rows(selected_date, course_id, staff=staff)
    school_weekdays = all_configured_school_weekdays()
    context = {
        'page_title': 'Attendance Not Taken',
        'courses': Course.objects.filter(id__in=teacher_course_ids(staff)).order_by('name'),
        'school_weekdays_json': json.dumps(school_weekdays),
        'selected_date': selected_date,
        'selected_course': course_id,
        'not_taken': not_taken,
    }
    return render(request, 'staff_template/staff_report_attendance_not_taken.html', context)


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
            message = form.cleaned_data['message']
            admins = list(CustomUser.objects.filter(admin__isnull=False))
            created = []
            skipped = []
            for d in form.cleaned_data['dates']:
                if LeaveReportStaff.objects.filter(staff=staff, date=d).exists():
                    skipped.append(d)
                    continue
                try:
                    LeaveReportStaff.objects.create(staff=staff, date=d, message=message)
                    created.append(d)
                except Exception:
                    logger.exception('Failed to create leave for %s on %s', staff, d)
                    skipped.append(d)
                    continue
                notif_message = f"{staff} applied for leave on {d}: {message}"
                # Admin has no in-app notification inbox, so email only.
                for admin_user in admins:
                    send_notification_email(admin_user, notif_message)
            return JsonResponse({'created': created, 'skipped': skipped})
        return JsonResponse({'errors': form.errors}, status=400)
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
            notify_student_leave_decision(leave, status)
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
                messages.error(request, "Please check the form - some required fields are missing or invalid.")
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


def delete_staff_notification(request, notification_id):
    staff = get_object_or_404(Staff, admin=request.user)
    try:
        # Scoped to `staff` so a teacher can't delete another teacher's
        # notification by guessing an id.
        notification = get_object_or_404(NotificationStaff, id=notification_id, staff=staff)
        notification.delete()
        return HttpResponse(True)
    except Exception:
        logger.exception("Failed to delete staff notification")
        return HttpResponse(False)


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
            # Scoped to `staff` so a staff member can't submit another
            # teacher's subject_id and edit grades they don't own.
            subject = get_object_or_404(Subject, id=subject_id, staff=staff)
            # Scoped to the subject's own class - otherwise any student id
            # in the school could have grades written against a subject
            # they never took.
            student = get_object_or_404(Student, id=student_id, course_id=subject.course_id)
            try:
                data = StudentResult.objects.get(
                    student=student, subject=subject)
                data.exam = exam
                data.test = test
                data.save()
                messages.success(request, "Scores Updated")
            except StudentResult.DoesNotExist:
                result = StudentResult(student=student, subject=subject, test=test, exam=exam)
                result.save()
                messages.success(request, "Scores Saved")
        except Exception as e:
            logger.exception('Unhandled error in staff_add_result')
            messages.warning(request, "Couldn't save your changes. Please check the form and try again.")
    return render(request, "staff_template/staff_add_result.html", context)


def fetch_student_result(request):
    try:
        staff = get_object_or_404(Staff, admin=request.user)
        subject_id = request.POST.get('subject')
        student_id = request.POST.get('student')
        # Scoped to `staff` so a staff member can't read another
        # teacher's subject results by guessing subject_id.
        subject = get_object_or_404(Subject, id=subject_id, staff=staff)
        # Scoped to the subject's own class, same reasoning as staff_add_result.
        student = get_object_or_404(Student, id=student_id, course_id=subject.course_id)
        result = StudentResult.objects.get(student=student, subject=subject)
        result_data = {
            'exam': result.exam,
            'test': result.test
        }
        return HttpResponse(json.dumps(result_data))
    except Exception as e:
        logger.exception('Unhandled error in fetch_student_result')
        return HttpResponse('False')
