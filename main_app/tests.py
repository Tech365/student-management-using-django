import datetime
import io
import json

from django.contrib.auth import authenticate
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image

from .forms import StaffForm
from .models import (Attendance, AttendanceReport, AuditLog, Course,
                     CustomUser, LeaveReportStaff, LeaveReportStudent,
                     NotificationParent, NotificationStaff,
                     NotificationStudent, Parent, ParentStudentLink, Session,
                     Staff, Student, StudentResult, Subject)
from .utils import grant_role, user_roles

PASSWORD = "TestPass9137!"  # must pass AUTH_PASSWORD_VALIDATORS - some tests submit it through a real form


def make_admin(email):
    return CustomUser.objects.create_superuser(
        email=email, password=PASSWORD, user_type=1,
        first_name="Bulk", last_name="Admin",
    )


def csv_file(content, name="upload.csv"):
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


def make_image_file(name="avatar.png"):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="white").save(buf, format="PNG")
    buf.seek(0)
    return SimpleUploadedFile(name, buf.read(), content_type="image/png")


def make_course(name="Computer Science"):
    return Course.objects.create(name=name)


def make_session():
    return Session.objects.create(
        start_year=datetime.date(2024, 1, 1),
        end_year=datetime.date(2024, 12, 31),
    )


def make_staff(email, course):
    user = CustomUser.objects.create_user(
        email=email, password=PASSWORD, user_type=2,
        first_name="Staff", last_name=email.split('@')[0],
    )
    staff = user.staff
    staff.course = course
    staff.save()
    return staff


def make_student(email, course, session):
    user = CustomUser.objects.create_user(
        email=email, password=PASSWORD, user_type=3,
        first_name="Student", last_name=email.split('@')[0],
    )
    student = user.student
    student.course = course
    student.session = session
    student.save()
    return student


def make_parent(email, linked_student=None):
    user = CustomUser.objects.create_user(
        email=email, password=PASSWORD, user_type=4,
        first_name="Parent", last_name=email.split('@')[0],
    )
    parent = user.parent
    if linked_student is not None:
        ParentStudentLink.objects.create(
            parent=parent, student=linked_student, relationship='father',
            date_of_birth=datetime.date(2015, 1, 1), status=1,
        )
    return parent


class UserProfileSignalTests(TestCase):
    def test_creating_staff_user_creates_staff_profile(self):
        user = CustomUser.objects.create_user(
            email="signal_staff@example.com", password=PASSWORD, user_type=2,
            first_name="Signal", last_name="Staff",
        )
        self.assertTrue(Staff.objects.filter(admin=user).exists())

    def test_creating_student_user_creates_student_profile(self):
        user = CustomUser.objects.create_user(
            email="signal_student@example.com", password=PASSWORD, user_type=3,
            first_name="Signal", last_name="Student",
        )
        self.assertTrue(Student.objects.filter(admin=user).exists())


class StaffMultipleClassesTests(TestCase):
    """A teacher's classes come entirely from which Subjects they're
    assigned to teach - a teacher can teach subjects in more than one
    class. Staff.course (a single field) is no longer part of the
    Add/Edit Staff form or shown anywhere; class membership is always
    derived from Subject assignments."""

    def setUp(self):
        self.session = make_session()
        self.course_a = make_course("Multi Class A")
        self.course_b = make_course("Multi Class B")
        self.teacher = make_staff("multi_class_teacher@example.com", self.course_a)
        Subject.objects.create(name="Subject A", staff=self.teacher, course=self.course_a)
        Subject.objects.create(name="Subject B", staff=self.teacher, course=self.course_b)
        self.student_a = make_student("multi_class_student_a@example.com", self.course_a, self.session)
        self.student_b = make_student("multi_class_student_b@example.com", self.course_b, self.session)

    def test_staff_form_has_no_class_field(self):
        self.assertNotIn('course', StaffForm().fields)

    def test_staff_home_counts_students_across_all_classes(self):
        self.client.login(username="multi_class_teacher@example.com", password=PASSWORD)
        response = self.client.get(reverse('staff_home'))
        self.assertEqual(response.context['total_students'], 2)
        self.assertIn('Multi Class A', response.context['page_title'])
        self.assertIn('Multi Class B', response.context['page_title'])

    def test_manage_staff_shows_all_classes(self):
        make_admin("multi_class_admin@example.com")
        self.client.login(username="multi_class_admin@example.com", password=PASSWORD)
        response = self.client.get(reverse('manage_staff'))
        by_email = {u.email: u for u in response.context['allStaff']}
        self.assertEqual(by_email["multi_class_teacher@example.com"].class_names,
                          "Multi Class A, Multi Class B")

    def test_add_staff_works_without_a_class(self):
        make_admin("multi_class_admin2@example.com")
        self.client.login(username="multi_class_admin2@example.com", password=PASSWORD)
        response = self.client.post(reverse('add_staff'), {
            'first_name': 'No', 'last_name': 'Class', 'email': 'no_class_teacher@example.com',
            'gender': 'M', 'password': PASSWORD, 'address': '1 Test St',
            'profile_pic': make_image_file(),
        })
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='no_class_teacher@example.com')
        self.assertIsNone(user.staff.course)


class RoleRedirectTests(TestCase):
    """LoginCheckMiddleWare enforces per-role page access."""

    def setUp(self):
        self.course = make_course()
        self.staff = make_staff("role_staff@example.com", self.course)

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(reverse('admin_home'))
        self.assertRedirects(response, reverse('login_page'))

    def test_staff_cannot_reach_hod_pages(self):
        self.client.login(username="role_staff@example.com", password=PASSWORD)
        response = self.client.get(reverse('admin_home'))
        self.assertRedirects(response, reverse('staff_home'))


class ResultPermissionTests(TestCase):
    """Regression tests for the IDOR in staff_add_result: a staff member
    must not be able to submit another teacher's subject id and write
    grades for a subject they don't teach."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.owner = make_staff("owner@example.com", self.course)
        self.intruder = make_staff("intruder@example.com", self.course)
        self.subject = Subject.objects.create(name="Maths", staff=self.owner, course=self.course)
        self.student = make_student("result_student@example.com", self.course, self.session)

    def test_staff_add_result_rejects_other_staffs_subject(self):
        self.client.login(username="intruder@example.com", password=PASSWORD)
        response = self.client.post(reverse('staff_add_result'), {
            'student_list': self.student.id,
            'subject': self.subject.id,
            'test': 10,
            'exam': 20,
        })
        # staff_add_result's own try/except turns the Http404 from the
        # ownership check into a normal 200 render with a flash message
        # (consistent with the rest of the view's error handling) — what
        # matters for this regression test is that no result gets written.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StudentResult.objects.filter(student=self.student, subject=self.subject).exists())

    def test_staff_add_result_allows_subject_owner(self):
        self.client.login(username="owner@example.com", password=PASSWORD)
        response = self.client.post(reverse('staff_add_result'), {
            'student_list': self.student.id,
            'subject': self.subject.id,
            'test': 10,
            'exam': 20,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(StudentResult.objects.filter(student=self.student, subject=self.subject).exists())

    def test_staff_add_result_rejects_student_outside_subjects_class(self):
        """Even the subject's own teacher can't write grades for a student
        who isn't enrolled in that subject's class."""
        other_course = make_course("Other Result Course")
        outsider = make_student("result_outsider@example.com", other_course, self.session)
        self.client.login(username="owner@example.com", password=PASSWORD)
        self.client.post(reverse('staff_add_result'), {
            'student_list': outsider.id,
            'subject': self.subject.id,
            'test': 10,
            'exam': 20,
        })
        self.assertFalse(StudentResult.objects.filter(student=outsider, subject=self.subject).exists())


class EditResultViewPermissionTests(TestCase):
    """Regression tests for the matching IDOR in EditResultView."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.owner = make_staff("owner2@example.com", self.course)
        self.intruder = make_staff("intruder2@example.com", self.course)
        self.subject = Subject.objects.create(name="Physics", staff=self.owner, course=self.course)
        self.student = make_student("edit_result_student@example.com", self.course, self.session)
        self.result = StudentResult.objects.create(student=self.student, subject=self.subject, test=5, exam=5)

    def _post(self, test, exam):
        return self.client.post(reverse('edit_student_result'), {
            'session_year': self.session.id,
            'subject': self.subject.id,
            'student': self.student.id,
            'test': test,
            'exam': exam,
        })

    def test_intruder_cannot_edit_other_staffs_result(self):
        self.client.login(username="intruder2@example.com", password=PASSWORD)
        self._post(99, 99)
        self.result.refresh_from_db()
        self.assertEqual(self.result.test, 5)
        self.assertEqual(self.result.exam, 5)

    def test_owner_can_edit_own_result(self):
        self.client.login(username="owner2@example.com", password=PASSWORD)
        self._post(88, 77)
        self.result.refresh_from_db()
        self.assertEqual(self.result.test, 88)
        self.assertEqual(self.result.exam, 77)

    def test_owner_cannot_edit_result_for_student_outside_subjects_class(self):
        other_course = make_course("Other Edit Result Course")
        outsider = make_student("edit_result_outsider@example.com", other_course, self.session)
        outside_result = StudentResult.objects.create(student=outsider, subject=self.subject, test=1, exam=1)
        self.client.login(username="owner2@example.com", password=PASSWORD)
        self.client.post(reverse('edit_student_result'), {
            'session_year': self.session.id, 'subject': self.subject.id,
            'student': outsider.id, 'test': 99, 'exam': 99,
        })
        outside_result.refresh_from_db()
        self.assertEqual(outside_result.test, 1)  # untouched


class AttendanceUpdateRoutingTests(TestCase):
    """Regression test for the duplicate `staff/attendance/update/` URL:
    the AJAX save endpoint (`update_attendance`) must be a distinct path
    from the page-render view (`staff_update_attendance`), and must
    actually persist the posted status."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.staff = make_staff("attendance_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="Chemistry", staff=self.staff, course=self.course)
        self.student = make_student("attendance_student@example.com", self.course, self.session)
        self.attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2024, 2, 1))
        self.report = AttendanceReport.objects.create(
            student=self.student, attendance=self.attendance, status=False)

    def test_update_attendance_has_its_own_url(self):
        self.assertNotEqual(reverse('update_attendance'), reverse('staff_update_attendance'))

    def test_update_attendance_persists_status(self):
        self.client.login(username="attendance_staff@example.com", password=PASSWORD)
        response = self.client.post(reverse('update_attendance'), {
            'date': self.attendance.id,
            'student_ids': json.dumps([{'id': self.student.admin_id, 'status': True}]),
        })
        self.assertEqual(response.content, b"OK")
        self.report.refresh_from_db()
        self.assertTrue(self.report.status)


class SaveAttendanceTests(TestCase):
    """Regression test: save_attendance (the Take Attendance screen) used
    to only write status on first creation of the AttendanceReport, so
    re-taking attendance for the same subject/session/date (e.g. to
    correct a mistake) silently kept the original status forever."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.staff = make_staff("save_attendance_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="Physics", staff=self.staff, course=self.course)
        self.student = make_student("save_attendance_student@example.com", self.course, self.session)
        self.client.login(username="save_attendance_staff@example.com", password=PASSWORD)

    def test_retaking_attendance_updates_existing_status(self):
        payload = {
            'date': '2026-08-08',
            'subject': self.subject.id,
            'session': self.session.id,
        }
        # First save: marked Present.
        self.client.post(reverse('save_attendance'), {
            **payload, 'student_ids': json.dumps([{'id': self.student.id, 'status': 1}]),
        })
        report = AttendanceReport.objects.get(student=self.student)
        self.assertTrue(report.status)

        # Second save for the same date/subject/session: corrected to Absent.
        self.client.post(reverse('save_attendance'), {
            **payload, 'student_ids': json.dumps([{'id': self.student.id, 'status': 0}]),
        })
        report.refresh_from_db()
        self.assertFalse(report.status)
        self.assertEqual(AttendanceReport.objects.filter(student=self.student).count(), 1)

    def test_approved_leave_student_not_recorded_even_if_submitted(self):
        LeaveReportStudent.objects.create(
            student=self.student, date="2026-08-08", message="Sick", status=1)
        self.client.post(reverse('save_attendance'), {
            'date': '2026-08-08', 'subject': self.subject.id, 'session': self.session.id,
            'student_ids': json.dumps([{'id': self.student.id, 'status': 1}]),
        })
        self.assertFalse(AttendanceReport.objects.filter(student=self.student).exists())

    def test_pending_leave_student_still_recorded(self):
        LeaveReportStudent.objects.create(
            student=self.student, date="2026-08-08", message="Sick", status=0)
        self.client.post(reverse('save_attendance'), {
            'date': '2026-08-08', 'subject': self.subject.id, 'session': self.session.id,
            'student_ids': json.dumps([{'id': self.student.id, 'status': 1}]),
        })
        self.assertTrue(AttendanceReport.objects.filter(student=self.student).exists())


class AttendanceEndpointsPermissionTests(TestCase):
    """Regression tests: get_students, save_attendance,
    get_student_attendance, and update_attendance never checked that the
    subject/attendance being acted on actually belonged to the logged-in
    staff member - any authenticated teacher could read or write another
    teacher's class's attendance by submitting a different subject id or
    attendance id, e.g. copied from that class's own page HTML."""

    def setUp(self):
        self.session = make_session()
        self.owner_course = make_course("Owner Course")
        self.intruder_course = make_course("Intruder Course")
        self.owner = make_staff("attendance_owner@example.com", self.owner_course)
        self.intruder = make_staff("attendance_intruder@example.com", self.intruder_course)
        self.owner_subject = Subject.objects.create(name="Owner Subject", staff=self.owner, course=self.owner_course)
        self.owner_student = make_student("attendance_owner_student@example.com", self.owner_course, self.session)
        self.attendance = Attendance.objects.create(
            session=self.session, subject=self.owner_subject, date=datetime.date(2026, 8, 8))
        self.report = AttendanceReport.objects.create(
            student=self.owner_student, attendance=self.attendance, status=True)
        self.client.login(username="attendance_intruder@example.com", password=PASSWORD)

    def test_get_students_rejects_other_teachers_subject(self):
        response = self.client.post(reverse('get_students'), {
            'subject': self.owner_subject.id, 'session': self.session.id, 'date': '2026-08-08',
        })
        self.assertEqual(response.status_code, 400)

    def test_save_attendance_rejects_other_teachers_subject(self):
        response = self.client.post(reverse('save_attendance'), {
            'date': '2026-08-08', 'subject': self.owner_subject.id, 'session': self.session.id,
            'student_ids': json.dumps([{'id': self.owner_student.id, 'status': 0}]),
        })
        self.assertEqual(response.content, b"False")
        self.report.refresh_from_db()
        self.assertTrue(self.report.status)  # untouched

    def test_get_student_attendance_rejects_other_teachers_attendance(self):
        response = self.client.post(reverse('get_student_attendance'), {
            'attendance_date_id': self.attendance.id,
        })
        self.assertEqual(response.status_code, 400)

    def test_update_attendance_rejects_other_teachers_attendance(self):
        response = self.client.post(reverse('update_attendance'), {
            'date': self.attendance.id,
            'student_ids': json.dumps([{'id': self.owner_student.admin_id, 'status': 0}]),
        })
        self.assertEqual(response.content, b"False")
        self.report.refresh_from_db()
        self.assertTrue(self.report.status)  # untouched

    def test_owner_can_still_use_their_own_endpoints(self):
        self.client.logout()
        self.client.login(username="attendance_owner@example.com", password=PASSWORD)
        response = self.client.post(reverse('get_students'), {
            'subject': self.owner_subject.id, 'session': self.session.id, 'date': '2026-08-08',
        })
        self.assertEqual(response.status_code, 200)

    def test_save_attendance_rejects_student_outside_subjects_class(self):
        """Even the subject's own teacher can't fabricate an attendance
        record for a student who isn't enrolled in that subject's class."""
        other_course = make_course("Other Attendance Course")
        outsider = make_student("attendance_outsider@example.com", other_course, self.session)
        self.client.logout()
        self.client.login(username="attendance_owner@example.com", password=PASSWORD)
        self.client.post(reverse('save_attendance'), {
            'date': '2026-08-09', 'subject': self.owner_subject.id, 'session': self.session.id,
            'student_ids': json.dumps([{'id': outsider.id, 'status': 1}]),
        })
        self.assertFalse(AttendanceReport.objects.filter(student=outsider).exists())

    def test_update_attendance_rejects_student_outside_subjects_class(self):
        other_course = make_course("Other Update Attendance Course")
        outsider = make_student("update_attendance_outsider@example.com", other_course, self.session)
        outside_report = AttendanceReport.objects.create(student=outsider, attendance=self.attendance, status=False)
        self.client.logout()
        self.client.login(username="attendance_owner@example.com", password=PASSWORD)
        self.client.post(reverse('update_attendance'), {
            'date': self.attendance.id,
            'student_ids': json.dumps([{'id': outsider.admin_id, 'status': 1}]),
        })
        outside_report.refresh_from_db()
        self.assertFalse(outside_report.status)  # untouched


class ViewUpdateAttendanceLeaveTests(TestCase):
    """A student on approved leave never gets an AttendanceReport (see
    SaveAttendanceTests), but should still show up - disabled - in
    View/Update Attendance instead of silently disappearing from the
    roster for that date."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.staff = make_staff("vu_attendance_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="Chemistry2", staff=self.staff, course=self.course)
        self.present_student = make_student("vu_present_student@example.com", self.course, self.session)
        self.leave_student = make_student("vu_leave_student@example.com", self.course, self.session)
        self.attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2026, 8, 8))
        AttendanceReport.objects.create(
            student=self.present_student, attendance=self.attendance, status=True)
        LeaveReportStudent.objects.create(
            student=self.leave_student, date="2026-08-08", message="Sick", status=1)
        self.client.login(username="vu_attendance_staff@example.com", password=PASSWORD)

    def test_on_leave_student_appears_flagged_with_no_status(self):
        response = self.client.post(reverse('get_student_attendance'), {
            'attendance_date_id': self.attendance.id,
        })
        rows = {row['id']: row for row in json.loads(response.json())}
        leave_row = rows[self.leave_student.admin_id]
        self.assertTrue(leave_row['on_leave'])
        self.assertIsNone(leave_row['status'])
        present_row = rows[self.present_student.admin_id]
        self.assertFalse(present_row['on_leave'])
        self.assertTrue(present_row['status'])

    def test_update_attendance_ignores_on_leave_student_even_if_submitted(self):
        self.client.post(reverse('update_attendance'), {
            'date': self.attendance.id,
            'student_ids': json.dumps([
                {'id': self.present_student.admin_id, 'status': 0},
                {'id': self.leave_student.admin_id, 'status': 1},
            ]),
        })
        self.assertFalse(AttendanceReport.objects.filter(student=self.present_student).first().status)
        self.assertFalse(AttendanceReport.objects.filter(student=self.leave_student).exists())


class AdminViewAttendanceLeaveTests(TestCase):
    """Same fix on the HOD/admin read-only attendance view."""

    def setUp(self):
        make_admin("admin_view_attendance_admin@example.com")
        self.client.login(username="admin_view_attendance_admin@example.com", password=PASSWORD)
        self.course = make_course()
        self.session = make_session()
        staff = make_staff("admin_view_attendance_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="Chemistry3", staff=staff, course=self.course)
        self.leave_student = make_student("admin_view_leave_student@example.com", self.course, self.session)
        self.attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2026, 8, 8))
        LeaveReportStudent.objects.create(
            student=self.leave_student, date="2026-08-08", message="Sick", status=1)

    def test_on_leave_student_shown_as_on_leave(self):
        response = self.client.post(reverse('get_admin_attendance'), {
            'subject': self.subject.id, 'session': self.session.id,
            'attendance_date_id': self.attendance.id,
        })
        rows = json.loads(response.json())
        self.assertEqual(rows[0]['status'], 'On Leave')


class StudentAndParentAttendanceHistoryLeaveTests(TestCase):
    """A student on approved leave never gets an AttendanceReport (see
    SaveAttendanceTests), so student_view_attendance/parent_view_attendance
    used to just silently drop those dates from the history instead of
    showing them as Leave the way the teacher's/admin's own attendance
    screens already do (see ViewUpdateAttendanceLeaveTests /
    AdminViewAttendanceLeaveTests)."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.staff = make_staff("history_leave_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="History", staff=self.staff, course=self.course)
        self.student = make_student("history_leave_student@example.com", self.course, self.session)
        self.present_attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2026, 8, 8))
        AttendanceReport.objects.create(
            student=self.student, attendance=self.present_attendance, status=True)
        self.leave_attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2026, 8, 9))
        LeaveReportStudent.objects.create(
            student=self.student, date="2026-08-09", message="Sick", status=1)

    def _fetch(self, url, extra=None):
        return self.client.post(reverse(url), {
            'subject': self.subject.id,
            'start_date': '2026-08-01', 'end_date': '2026-08-31',
            **(extra or {}),
        })

    def test_student_attendance_history_shows_leave_day(self):
        self.client.login(username="history_leave_student@example.com", password=PASSWORD)
        response = self._fetch('student_view_attendance')
        rows = {row['date']: row['status'] for row in json.loads(response.json())}
        self.assertEqual(rows['2026-08-08'], True)
        self.assertEqual(rows['2026-08-09'], 'leave')

    def test_parent_attendance_history_shows_leave_day(self):
        parent = make_parent("history_leave_parent@example.com", linked_student=self.student)
        self.client.login(username="history_leave_parent@example.com", password=PASSWORD)
        response = self._fetch('parent_view_attendance', {'student': self.student.id})
        rows = {row['date']: row['status'] for row in json.loads(response.json())}
        self.assertEqual(rows['2026-08-08'], True)
        self.assertEqual(rows['2026-08-09'], 'leave')


class StudentViewAttendanceCourselessTests(TestCase):
    """Regression test: student_view_attendance's GET branch dereferenced
    student.course.id directly, crashing with an AttributeError (500) for
    a student whose course was never set/was cleared - course=None is an
    explicitly reachable, otherwise-handled state elsewhere in the app."""

    def test_courseless_student_sees_empty_page_not_500(self):
        course = make_course()
        session = make_session()
        student = make_student("courseless_view_attendance@example.com", course, session)
        student.course = None
        student.save()
        self.client.login(username="courseless_view_attendance@example.com", password=PASSWORD)
        response = self.client.get(reverse('student_view_attendance'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['subjects']), [])


class CsrfEnforcementTests(TestCase):
    """Regression test for the AJAX endpoints that used to be @csrf_exempt:
    they must now reject requests without a valid CSRF token."""

    def setUp(self):
        self.course = make_course()
        self.session = make_session()
        self.staff = make_staff("csrf_staff@example.com", self.course)
        Subject.objects.create(name="Biology", staff=self.staff, course=self.course)

    def test_get_students_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.login(username="csrf_staff@example.com", password=PASSWORD)
        response = client.post(reverse('get_students'), {
            'subject': Subject.objects.get(name="Biology").id,
            'session': self.session.id,
        })
        self.assertEqual(response.status_code, 403)


class BulkUploadCourseTests(TestCase):
    def setUp(self):
        make_admin("bulk_admin_course@example.com")
        self.client.login(username="bulk_admin_course@example.com", password=PASSWORD)

    def test_creates_new_courses(self):
        content = "name\nClass A\nClass B\n"
        self.client.post(reverse('bulk_upload_courses'), {'csv_file': csv_file(content)})
        self.assertTrue(Course.objects.filter(name="Class A").exists())
        self.assertTrue(Course.objects.filter(name="Class B").exists())

    def test_skips_duplicate_course(self):
        Course.objects.create(name="Class A")
        response = self.client.post(reverse('bulk_upload_courses'), {'csv_file': csv_file("name\nClass A\n")})
        self.assertEqual(response.context['results'][0]['status'], 'skipped')
        self.assertEqual(Course.objects.filter(name="Class A").count(), 1)

    def test_missing_name_reported_as_error(self):
        response = self.client.post(reverse('bulk_upload_courses'), {'csv_file': csv_file("name\n \n")})
        self.assertEqual(response.context['results'][0]['status'], 'error')

    def test_staff_cannot_reach_bulk_upload(self):
        course = make_course("Perm Check Course")
        make_staff("bulk_perm_staff@example.com", course)
        client = Client()
        client.login(username="bulk_perm_staff@example.com", password=PASSWORD)
        response = client.get(reverse('bulk_upload_courses'))
        self.assertRedirects(response, reverse('staff_home'))


class BulkUploadSubjectTests(TestCase):
    def setUp(self):
        make_admin("bulk_admin_subject@example.com")
        self.client.login(username="bulk_admin_subject@example.com", password=PASSWORD)
        self.course = make_course("Bulk Subject Course")
        self.staff = make_staff("bulk_teacher@example.com", self.course)

    def test_creates_subject_with_valid_references(self):
        content = "name,staff_email,class\nQuran,bulk_teacher@example.com,Bulk Subject Course\n"
        self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertTrue(Subject.objects.filter(name="Quran", course=self.course, staff=self.staff).exists())

    def test_unknown_staff_email_reported_as_error(self):
        content = "name,staff_email,class\nQuran,missing@example.com,Bulk Subject Course\n"
        response = self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(Subject.objects.filter(name="Quran").exists())

    def test_unknown_course_reported_as_error(self):
        content = "name,staff_email,class\nQuran,bulk_teacher@example.com,No Such Course\n"
        response = self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')


class BulkUploadStaffTests(TestCase):
    """A staff member's classes come from Subject assignments (see
    ManageStaffClassNamesTests), not from the bulk-upload CSV - there's
    no class/course column here at all."""

    def setUp(self):
        make_admin("bulk_admin_staff@example.com")
        self.client.login(username="bulk_admin_staff@example.com", password=PASSWORD)
        self.course = make_course("Staff Bulk Course")

    def test_creates_staff_account_with_working_generated_password(self):
        content = ("first_name,last_name,email,gender,address\n"
                   "New,Teacher,new_teacher@example.com,F,1 Test St\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        result = response.context['results'][0]
        self.assertEqual(result['status'], 'success')
        user = CustomUser.objects.get(email="new_teacher@example.com")
        self.assertEqual(user.user_type, '2')
        generated_password = result['message'].split(': ')[-1]
        self.assertTrue(user.check_password(generated_password))

    def test_invalid_gender_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address\n"
                   "Bad,Gender,bad_gender@example.com,X,addr\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(CustomUser.objects.filter(email="bad_gender@example.com").exists())

    def test_missing_field_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address\n"
                   ",Gender,no_first_name@example.com,M,addr\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')

    def test_duplicate_email_skipped(self):
        make_staff("dup_staff@example.com", self.course)
        content = ("first_name,last_name,email,gender,address\n"
                   "Dup,Staff,dup_staff@example.com,M,addr\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'skipped')


class BulkUploadStudentTests(TestCase):
    def setUp(self):
        make_admin("bulk_admin_student@example.com")
        self.client.login(username="bulk_admin_student@example.com", password=PASSWORD)
        self.course = make_course("Student Bulk Course")
        self.session = make_session()

    def test_creates_student_account(self):
        content = ("first_name,last_name,email,gender,address,class,session_start_year,session_end_year\n"
                   "New,Student,new_student@example.com,M,1 Test St,Student Bulk Course,2024-01-01,2024-12-31\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        result = response.context['results'][0]
        self.assertEqual(result['status'], 'success')
        user = CustomUser.objects.get(email="new_student@example.com")
        self.assertEqual(user.student.course, self.course)
        self.assertEqual(user.student.session, self.session)
        generated_password = result['message'].split(': ')[-1]
        self.assertTrue(user.check_password(generated_password))

    def test_unknown_session_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address,class,session_start_year,session_end_year\n"
                   "New,Student,no_session@example.com,M,1 Test St,Student Bulk Course,1999-01-01,1999-12-31\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(CustomUser.objects.filter(email="no_session@example.com").exists())

    def test_bad_date_format_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address,class,session_start_year,session_end_year\n"
                   "Bad,Date,bad_date@example.com,M,1 Test St,Student Bulk Course,not-a-date,also-not\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')

    def test_duplicate_email_skipped(self):
        make_student("dup_student@example.com", self.course, self.session)
        content = ("first_name,last_name,email,gender,address,class,session_start_year,session_end_year\n"
                   "Dup,Student,dup_student@example.com,M,1 Test St,Student Bulk Course,2024-01-01,2024-12-31\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'skipped')


class DeleteWithAttendanceHistoryTests(TestCase):
    """Regression tests: deleting a student/staff/subject/session that has
    real attendance history used to 500 (on_delete=DO_NOTHING against a
    database that actually enforces FK constraints, e.g. Postgres in prod).
    Deleting should now succeed and clean up the dependent attendance rows."""

    def setUp(self):
        make_admin("delete_admin@example.com")
        self.client.login(username="delete_admin@example.com", password=PASSWORD)
        self.course = make_course("Delete Test Course")
        self.session = make_session()
        self.staff = make_staff("delete_teacher@example.com", self.course)
        self.subject = Subject.objects.create(name="Delete Test Subject", staff=self.staff, course=self.course)
        self.student = make_student("delete_student@example.com", self.course, self.session)
        attendance = Attendance.objects.create(session=self.session, subject=self.subject, date=datetime.date(2026, 1, 1))
        AttendanceReport.objects.create(student=self.student, attendance=attendance, status=True)

    def test_deleting_student_with_attendance_history_succeeds(self):
        response = self.client.get(reverse('delete_student', args=[self.student.id]))
        self.assertRedirects(response, reverse('manage_student'))
        self.assertFalse(Student.objects.filter(id=self.student.id).exists())
        self.assertFalse(AttendanceReport.objects.filter(student_id=self.student.id).exists())

    def test_deleting_staff_with_attendance_history_succeeds(self):
        response = self.client.get(reverse('delete_staff', args=[self.staff.id]))
        self.assertRedirects(response, reverse('manage_staff'))
        self.assertFalse(Staff.objects.filter(id=self.staff.id).exists())
        self.assertFalse(Subject.objects.filter(id=self.subject.id).exists())
        self.assertFalse(Attendance.objects.filter(subject_id=self.subject.id).exists())

    def test_deleting_subject_with_attendance_history_succeeds(self):
        response = self.client.get(reverse('delete_subject', args=[self.subject.id]))
        self.assertRedirects(response, reverse('manage_subject'))
        self.assertFalse(Subject.objects.filter(id=self.subject.id).exists())
        self.assertFalse(Attendance.objects.filter(subject_id=self.subject.id).exists())

    def test_deleting_course_with_enrolled_student_is_blocked_not_500(self):
        response = self.client.get(reverse('delete_course', args=[self.course.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Course.objects.filter(id=self.course.id).exists())

    def test_deleting_session_with_attendance_history_is_blocked_not_500(self):
        response = self.client.get(reverse('delete_session', args=[self.session.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Session.objects.filter(id=self.session.id).exists())


class ReportPermissionTests(TestCase):
    def test_staff_cannot_reach_reports(self):
        course = make_course("Report Perm Course")
        make_staff("report_perm_staff@example.com", course)
        client = Client()
        client.login(username="report_perm_staff@example.com", password=PASSWORD)
        response = client.get(reverse('report_attendance_summary'))
        self.assertRedirects(response, reverse('staff_home'))


class AttendanceSummaryReportTests(TestCase):
    def setUp(self):
        make_admin("report_admin_attendance@example.com")
        self.client.login(username="report_admin_attendance@example.com", password=PASSWORD)
        self.course = make_course("Report Course")
        self.session = make_session()
        self.staff = make_staff("report_teacher@example.com", self.course)
        self.subject = Subject.objects.create(name="Report Subject", staff=self.staff, course=self.course)
        self.student = make_student("report_student@example.com", self.course, self.session)
        attendance = Attendance.objects.create(session=self.session, subject=self.subject, date=datetime.date(2024, 6, 15))
        AttendanceReport.objects.create(student=self.student, attendance=attendance, status=True)
        other_student = make_student("report_student2@example.com", self.course, self.session)
        AttendanceReport.objects.create(student=other_student, attendance=attendance, status=False)

    def test_summary_shows_correct_percentage(self):
        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        self.assertEqual(response.status_code, 200)
        summary = response.context['course_summary']
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]['total'], 2)
        self.assertEqual(summary[0]['present'], 1)
        self.assertEqual(summary[0]['percent'], 50.0)

    def test_csv_export_returns_csv(self):
        response = self.client.get(reverse('report_attendance_summary_csv'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        body = response.content.decode()
        self.assertIn('Report Course', body)
        self.assertIn('50.0%', body)

    def test_by_subject_breakdown_and_filter(self):
        # A 2nd subject in the same course/date with a different
        # attendance rate - the by-subject table must keep them separate
        # instead of blending into one course-wide number.
        second_subject = Subject.objects.create(name="Second Subject", staff=self.staff, course=self.course)
        attendance2 = Attendance.objects.create(
            session=self.session, subject=second_subject, date=datetime.date(2024, 6, 15))
        AttendanceReport.objects.create(student=self.student, attendance=attendance2, status=False)

        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        by_name = {r['subject']: r for r in response.context['subject_summary']}
        self.assertEqual(by_name['Report Subject']['total'], 2)
        self.assertEqual(by_name['Report Subject']['percent'], 50.0)
        self.assertEqual(by_name['Second Subject']['total'], 1)
        self.assertEqual(by_name['Second Subject']['percent'], 0.0)

        # Filtering by subject should scope the course/day/student tables too.
        filtered = self.client.get(reverse('report_attendance_summary'), {
            'subject': second_subject.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        self.assertEqual(filtered.context['course_summary'][0]['total'], 1)

        csv_response = self.client.get(reverse('report_attendance_by_subject_csv'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        body = csv_response.content.decode()
        self.assertIn('Second Subject', body)
        self.assertIn('Report Subject', body)

    def test_approved_leave_day_excluded_from_percentage(self):
        # A 3rd student, absent only because of an approved leave, should
        # not drag the percentage down or count toward the total.
        leave_student = make_student("report_leave_excl_student@example.com", self.course, self.session)
        attendance = Attendance.objects.get(date=datetime.date(2024, 6, 15))
        AttendanceReport.objects.create(student=leave_student, attendance=attendance, status=False)
        LeaveReportStudent.objects.create(
            student=leave_student, date="2024-06-15", message="Sick", status=1)

        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        summary = response.context['course_summary']
        self.assertEqual(summary[0]['total'], 2)
        self.assertEqual(summary[0]['present'], 1)
        self.assertEqual(summary[0]['percent'], 50.0)
        self.assertEqual(summary[0]['leave'], 1)

        student_row = {r['name']: r for r in response.context['student_summary']}[str(leave_student)]
        self.assertEqual(student_row['leave'], 1)
        day_row = response.context['daily_summary'][0]
        self.assertEqual(day_row['leave'], 1)

        csv_response = self.client.get(reverse('report_attendance_summary_csv'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        self.assertIn('Leave', csv_response.content.decode())

    def test_student_with_only_leave_records_still_shown_not_flagged(self):
        # A student whose ONLY record in range is an approved leave (no
        # AttendanceReport at all, matching current Take Attendance
        # behavior) must not just vanish from the by-student table.
        leave_only_student = make_student("report_leave_only_student@example.com", self.course, self.session)
        LeaveReportStudent.objects.create(
            student=leave_only_student, date="2024-06-15", message="Sick", status=1)

        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        by_name = {r['name']: r for r in response.context['student_summary']}
        row = by_name[str(leave_only_student)]
        self.assertEqual(row['total'], 0)
        self.assertEqual(row['leave'], 1)
        self.assertFalse(row['flagged'])

    def test_pending_leave_still_counts_as_absent(self):
        leave_student = make_student("report_pending_leave_student@example.com", self.course, self.session)
        attendance = Attendance.objects.get(date=datetime.date(2024, 6, 15))
        AttendanceReport.objects.create(student=leave_student, attendance=attendance, status=False)
        LeaveReportStudent.objects.create(
            student=leave_student, date="2024-06-15", message="Sick", status=0)

        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        summary = response.context['course_summary']
        self.assertEqual(summary[0]['total'], 3)
        self.assertEqual(summary[0]['present'], 1)


class StudentAttendanceReportTests(TestCase):
    def setUp(self):
        make_admin("report_admin_student@example.com")
        self.client.login(username="report_admin_student@example.com", password=PASSWORD)
        self.course = make_course("Student Report Course")
        self.session = make_session()
        self.staff = make_staff("report_teacher2@example.com", self.course)
        self.subject = Subject.objects.create(name="Student Report Subject", staff=self.staff, course=self.course)
        self.student = make_student("report_target_student@example.com", self.course, self.session)
        attendance = Attendance.objects.create(session=self.session, subject=self.subject, date=datetime.date(2024, 6, 15))
        AttendanceReport.objects.create(student=self.student, attendance=attendance, status=True)

    def test_no_student_selected_shows_empty_state(self):
        response = self.client.get(reverse('report_student_attendance'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['summary'])

    def test_selecting_student_shows_summary(self):
        response = self.client.get(reverse('report_student_attendance'), {'student': self.student.id})
        self.assertEqual(response.status_code, 200)
        summary = response.context['summary']
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['present'], 1)
        self.assertEqual(summary['percent'], 100.0)

    def test_csv_requires_student(self):
        response = self.client.get(reverse('report_student_attendance_csv'))
        self.assertEqual(response.status_code, 400)

    def test_by_subject_breakdown_for_student(self):
        second_subject = Subject.objects.create(name="Second Subject", staff=self.staff, course=self.course)
        attendance2 = Attendance.objects.create(
            session=self.session, subject=second_subject, date=datetime.date(2024, 6, 16))
        AttendanceReport.objects.create(student=self.student, attendance=attendance2, status=False)

        response = self.client.get(reverse('report_student_attendance'), {'student': self.student.id})
        by_name = {r['subject']: r for r in response.context['subject_summary']}
        self.assertEqual(by_name['Student Report Subject']['total'], 1)
        self.assertEqual(by_name['Student Report Subject']['percent'], 100.0)
        self.assertEqual(by_name['Second Subject']['total'], 1)
        self.assertEqual(by_name['Second Subject']['percent'], 0.0)

    def test_approved_leave_excluded_from_summary_but_visible_in_history(self):
        leave_attendance = Attendance.objects.create(
            session=self.session, subject=self.subject, date=datetime.date(2024, 6, 20))
        AttendanceReport.objects.create(student=self.student, attendance=leave_attendance, status=False)
        LeaveReportStudent.objects.create(
            student=self.student, date="2024-06-20", message="Sick", status=1)

        response = self.client.get(reverse('report_student_attendance'), {'student': self.student.id})
        summary = response.context['summary']
        # Still 1/1 = 100%: the leave-day absence doesn't count.
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['present'], 1)
        self.assertEqual(summary['percent'], 100.0)
        self.assertEqual(summary['leave'], 1)
        # But the leave day's own attendance row is still visible in the
        # raw history list for auditing.
        self.assertEqual(len(response.context['records']), 2)

    def test_csv_export_with_student(self):
        response = self.client.get(reverse('report_student_attendance_csv'), {'student': self.student.id})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Present', response.content.decode())


class LeaveReportTests(TestCase):
    def setUp(self):
        make_admin("report_admin_leave@example.com")
        self.client.login(username="report_admin_leave@example.com", password=PASSWORD)
        course = make_course("Leave Report Course")
        session = make_session()
        self.staff = make_staff("report_leave_staff@example.com", course)
        self.student = make_student("report_leave_student@example.com", course, session)
        LeaveReportStudent.objects.create(student=self.student, date="2024-06-10", message="Sick", status=0)
        LeaveReportStaff.objects.create(staff=self.staff, date="2024-06-11", message="Family event", status=1)

    def test_all_records_shown_by_default(self):
        response = self.client.get(reverse('report_leave'))
        self.assertEqual(len(response.context['records']), 2)

    def test_filter_by_type_student(self):
        response = self.client.get(reverse('report_leave'), {'type': 'student'})
        records = response.context['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['type'], 'Student')

    def test_filter_by_status_pending(self):
        response = self.client.get(reverse('report_leave'), {'status': 'pending'})
        records = response.context['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status_label'], 'Pending')

    def test_csv_export(self):
        response = self.client.get(reverse('report_leave_csv'))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Sick', body)
        self.assertIn('Family event', body)


class StaffStudentLeaveApprovalTests(TestCase):
    """A student's leave should be approved by one of their class's
    teachers - every Staff who teaches at least one Subject in the
    student's course, since a class can have more than one teacher and a
    teacher can teach in more than one class - not only by the HOD. Also
    guards against a teacher approving another class's leave by guessing
    a LeaveReportStudent id."""

    def setUp(self):
        self.session = make_session()
        self.course_a = make_course("Leave Approval Course A")
        self.course_b = make_course("Leave Approval Course B")
        self.teacher = make_staff("leave_teacher@example.com", self.course_a)
        Subject.objects.create(name="Course A Subject", staff=self.teacher, course=self.course_a)
        self.student_a = make_student("leave_student_a@example.com", self.course_a, self.session)
        self.student_b = make_student("leave_student_b@example.com", self.course_b, self.session)
        self.leave_a = LeaveReportStudent.objects.create(
            student=self.student_a, date="2024-06-10", message="Sick", status=0)
        self.leave_b = LeaveReportStudent.objects.create(
            student=self.student_b, date="2024-06-11", message="Family event", status=0)
        self.client.login(username="leave_teacher@example.com", password=PASSWORD)

    def test_teacher_sees_only_their_class(self):
        response = self.client.get(reverse('staff_view_student_leave'))
        leaves = list(response.context['allLeave'])
        self.assertEqual(leaves, [self.leave_a])

    def test_teacher_can_approve_own_class_leave(self):
        response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave_a.id, 'status': '1',
        })
        self.assertEqual(response.content.decode(), 'True')
        self.leave_a.refresh_from_db()
        self.assertEqual(self.leave_a.status, 1)

    def test_teacher_cannot_approve_other_class_leave(self):
        response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave_b.id, 'status': '1',
        })
        self.assertEqual(response.content.decode(), 'False')
        self.leave_b.refresh_from_db()
        self.assertEqual(self.leave_b.status, 0)

    def test_already_decided_leave_cannot_be_redecided(self):
        # Simulates the admin (or a duplicate click) deciding a leave
        # request the teacher's page is also showing as still pending.
        self.leave_a.status = 1
        self.leave_a.save()
        mail.outbox = []
        response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave_a.id, 'status': '-1',
        })
        self.assertEqual(response.content.decode(), 'False')
        self.leave_a.refresh_from_db()
        self.assertEqual(self.leave_a.status, 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_second_teacher_of_same_class_can_also_approve(self):
        # A class can have more than one teacher (e.g. a madrasa with two
        # teachers per class, each covering different subjects).
        second_teacher = make_staff("leave_teacher_2@example.com", self.course_b)
        Subject.objects.create(name="Course A Subject 2", staff=second_teacher, course=self.course_a)
        self.client.logout()
        self.client.login(username="leave_teacher_2@example.com", password=PASSWORD)
        response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave_a.id, 'status': '1',
        })
        self.assertEqual(response.content.decode(), 'True')
        self.leave_a.refresh_from_db()
        self.assertEqual(self.leave_a.status, 1)

    def test_teacher_of_multiple_classes_sees_leave_from_all_of_them(self):
        # Same teacher can teach a subject in more than one class.
        Subject.objects.create(name="Course B Subject", staff=self.teacher, course=self.course_b)
        response = self.client.get(reverse('staff_view_student_leave'))
        leaves = set(response.context['allLeave'])
        self.assertEqual(leaves, {self.leave_a, self.leave_b})

    def test_teacher_with_no_subjects_sees_and_approves_nothing(self):
        no_subject_teacher = make_staff("leave_teacher_none@example.com", self.course_a)
        self.client.logout()
        self.client.login(username="leave_teacher_none@example.com", password=PASSWORD)
        response = self.client.get(reverse('staff_view_student_leave'))
        self.assertEqual(list(response.context['allLeave']), [])
        post_response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave_a.id, 'status': '1',
        })
        self.assertEqual(post_response.content.decode(), 'False')


class CourselessStudentLeaveScopingTests(TestCase):
    """Class-teacher scoping is derived from Subject (staff, course), and
    Subject.course is never null - so a student with course=None can
    never match any teacher's taught courses. Covers the same fail-open
    risk the old Staff.course-based scoping had (filtering by course=None
    would otherwise match every other courseless record), now from the
    student side; the teacher side is covered by
    StaffStudentLeaveApprovalTests.test_teacher_with_no_subjects_sees_and_approves_nothing."""

    def setUp(self):
        self.session = make_session()
        self.course = make_course("Null Scoping Course")
        self.teacher = make_staff("courseless_scoping_teacher@example.com", self.course)
        Subject.objects.create(name="Some Subject", staff=self.teacher, course=self.course)
        self.courseless_student = make_student("courseless_student@example.com", self.course, self.session)
        self.courseless_student.course = None
        self.courseless_student.save()
        self.leave = LeaveReportStudent.objects.create(
            student=self.courseless_student, date="2024-06-10", message="Sick", status=0)

    def test_teacher_cannot_see_courseless_students_leave(self):
        self.client.login(username="courseless_scoping_teacher@example.com", password=PASSWORD)
        response = self.client.get(reverse('staff_view_student_leave'))
        self.assertEqual(list(response.context['allLeave']), [])

    def test_teacher_cannot_approve_courseless_students_leave(self):
        self.client.login(username="courseless_scoping_teacher@example.com", password=PASSWORD)
        response = self.client.post(reverse('staff_view_student_leave'), {
            'id': self.leave.id, 'status': '1',
        })
        self.assertEqual(response.content.decode(), 'False')
        self.leave.refresh_from_db()
        self.assertEqual(self.leave.status, 0)

    def test_courseless_student_applying_notifies_nobody(self):
        self.client.login(username="courseless_student@example.com", password=PASSWORD)
        mail.outbox = []
        self.client.post(reverse('student_apply_leave'), {
            'date': '2024-06-12', 'message': 'Feeling unwell',
        })
        self.assertFalse(NotificationStaff.objects.filter(staff=self.teacher).exists())
        self.assertEqual(len(mail.outbox), 0)


class TakeAttendanceLeaveFlagTests(TestCase):
    """get_students (used by the take-attendance screen) should flag
    students with an approved leave for the selected date, so a teacher
    doesn't default-mark them present without realizing."""

    def setUp(self):
        self.course = make_course("Attendance Leave Course")
        self.session = make_session()
        self.teacher = make_staff("attendance_leave_teacher@example.com", self.course)
        self.subject = Subject.objects.create(name="Attendance Leave Subject", staff=self.teacher, course=self.course)
        self.on_leave_student = make_student("attendance_leave_on@example.com", self.course, self.session)
        self.other_student = make_student("attendance_leave_off@example.com", self.course, self.session)
        LeaveReportStudent.objects.create(
            student=self.on_leave_student, date="2024-06-15", message="Sick", status=1)
        self.client.login(username="attendance_leave_teacher@example.com", password=PASSWORD)

    def test_approved_leave_on_date_is_flagged(self):
        response = self.client.post(reverse('get_students'), {
            'subject': self.subject.id, 'session': self.session.id, 'date': '2024-06-15',
        })
        data = {row['id']: row['on_leave'] for row in json.loads(response.json())}
        self.assertTrue(data[self.on_leave_student.id])
        self.assertFalse(data[self.other_student.id])

    def test_leave_on_a_different_date_not_flagged(self):
        response = self.client.post(reverse('get_students'), {
            'subject': self.subject.id, 'session': self.session.id, 'date': '2024-06-16',
        })
        data = {row['id']: row['on_leave'] for row in json.loads(response.json())}
        self.assertFalse(data[self.on_leave_student.id])

    def test_pending_leave_not_flagged(self):
        LeaveReportStudent.objects.filter(student=self.on_leave_student).update(status=0)
        response = self.client.post(reverse('get_students'), {
            'subject': self.subject.id, 'session': self.session.id, 'date': '2024-06-15',
        })
        data = {row['id']: row['on_leave'] for row in json.loads(response.json())}
        self.assertFalse(data[self.on_leave_student.id])


class LeaveNotificationTests(TestCase):
    """Applying for leave, and deciding on it, should notify whoever
    needs to act/know next - previously nobody was told and had to check
    manually."""

    def setUp(self):
        self.course = make_course("Leave Notify Course")
        self.session = make_session()
        self.teacher = make_staff("leave_notify_teacher@example.com", self.course)
        Subject.objects.create(name="Leave Notify Subject", staff=self.teacher, course=self.course)
        self.student = make_student("leave_notify_student@example.com", self.course, self.session)
        self.admin_user = make_admin("leave_notify_admin@example.com")

    def test_student_applying_notifies_class_teacher(self):
        self.client.login(username="leave_notify_student@example.com", password=PASSWORD)
        mail.outbox = []
        response = self.client.post(reverse('student_apply_leave'), {
            'date': '2024-06-15', 'message': 'Feeling unwell',
        })
        self.assertEqual(response.status_code, 302)
        notif = NotificationStaff.objects.get(staff=self.teacher)
        self.assertIn('Feeling unwell', notif.message)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.teacher.admin.email])

    def test_teacher_decision_notifies_student(self):
        leave = LeaveReportStudent.objects.create(
            student=self.student, date="2024-06-15", message="Sick", status=0)
        self.client.login(username="leave_notify_teacher@example.com", password=PASSWORD)
        mail.outbox = []
        self.client.post(reverse('staff_view_student_leave'), {'id': leave.id, 'status': '1'})
        notif = NotificationStudent.objects.get(student=self.student)
        self.assertIn('approved', notif.message)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.student.admin.email])

    def test_admin_decision_notifies_student(self):
        leave = LeaveReportStudent.objects.create(
            student=self.student, date="2024-06-15", message="Sick", status=0)
        self.client.login(username="leave_notify_admin@example.com", password=PASSWORD)
        mail.outbox = []
        self.client.post(reverse('view_student_leave'), {'id': leave.id, 'status': '-1'})
        notif = NotificationStudent.objects.get(student=self.student)
        self.assertIn('rejected', notif.message)
        self.assertEqual(len(mail.outbox), 1)

    def test_staff_applying_notifies_admin_by_email_only(self):
        self.client.login(username="leave_notify_teacher@example.com", password=PASSWORD)
        mail.outbox = []
        response = self.client.post(reverse('staff_apply_leave'), {
            'date': '2024-06-15', 'message': 'Family event',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.admin_user.email])
        self.assertIn('Family event', mail.outbox[0].body)

    def test_admin_decision_on_staff_leave_notifies_staff(self):
        leave = LeaveReportStaff.objects.create(
            staff=self.teacher, date="2024-06-15", message="Family event", status=0)
        self.client.login(username="leave_notify_admin@example.com", password=PASSWORD)
        mail.outbox = []
        self.client.post(reverse('view_staff_leave'), {'id': leave.id, 'status': '1'})
        notif = NotificationStaff.objects.get(staff=self.teacher, message__icontains="approved")
        self.assertIsNotNone(notif)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.teacher.admin.email])

    def test_admin_cannot_redecide_already_decided_student_leave(self):
        # e.g. the teacher already approved it via staff_view_student_leave
        # before the admin's page (also showing it as pending) submits.
        leave = LeaveReportStudent.objects.create(
            student=self.student, date="2024-06-15", message="Sick", status=1)
        self.client.login(username="leave_notify_admin@example.com", password=PASSWORD)
        mail.outbox = []
        response = self.client.post(reverse('view_student_leave'), {'id': leave.id, 'status': '-1'})
        self.assertEqual(response.content.decode(), 'False')
        leave.refresh_from_db()
        self.assertEqual(leave.status, 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_cannot_redecide_already_decided_staff_leave(self):
        leave = LeaveReportStaff.objects.create(
            staff=self.teacher, date="2024-06-15", message="Family event", status=-1)
        self.client.login(username="leave_notify_admin@example.com", password=PASSWORD)
        mail.outbox = []
        response = self.client.post(reverse('view_staff_leave'), {'id': leave.id, 'status': '1'})
        self.assertEqual(response.content.decode(), 'False')
        leave.refresh_from_db()
        self.assertEqual(leave.status, -1)
        self.assertEqual(len(mail.outbox), 0)


class ResultsReportTests(TestCase):
    def setUp(self):
        make_admin("report_admin_results@example.com")
        self.client.login(username="report_admin_results@example.com", password=PASSWORD)
        self.course = make_course("Results Report Course")
        session = make_session()
        self.staff = make_staff("report_results_staff@example.com", self.course)
        self.subject = Subject.objects.create(name="Results Report Subject", staff=self.staff, course=self.course)
        self.student = make_student("report_results_student@example.com", self.course, session)
        StudentResult.objects.create(student=self.student, subject=self.subject, test=8, exam=16)

    def test_summary_shows_averages(self):
        response = self.client.get(reverse('report_results'), {'course': self.course.id})
        summary = response.context['summary']
        self.assertEqual(summary['count'], 1)
        self.assertEqual(summary['avg_test'], 8.0)
        self.assertEqual(summary['avg_exam'], 16.0)

    def test_no_results_gives_no_summary(self):
        other_course = make_course("Empty Results Course")
        response = self.client.get(reverse('report_results'), {'course': other_course.id})
        self.assertIsNone(response.context['summary'])

    def test_csv_export(self):
        response = self.client.get(reverse('report_results_csv'), {'course': self.course.id})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Results Report Subject', body)


class AddAdminTests(TestCase):
    def setUp(self):
        make_admin("add_admin_actor@example.com")
        self.client.login(username="add_admin_actor@example.com", password=PASSWORD)

    def _post(self, email):
        return self.client.post(reverse('add_admin'), {
            'first_name': 'New', 'last_name': 'Principal', 'email': email,
            'gender': 'F', 'address': '1 Test St', 'password': PASSWORD,
            'profile_pic': make_image_file(),
        })

    def test_creates_hod_account_without_django_admin_access(self):
        self._post('new_principal@example.com')
        user = CustomUser.objects.get(email='new_principal@example.com')
        self.assertEqual(user.user_type, '1')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(PASSWORD))

    def test_new_admin_can_log_in_and_reach_admin_home(self):
        self._post('new_principal2@example.com')
        client = Client()
        ok = client.login(username='new_principal2@example.com', password=PASSWORD)
        self.assertTrue(ok)
        response = client.get(reverse('admin_home'))
        self.assertEqual(response.status_code, 200)

    def test_staff_cannot_reach_add_admin(self):
        course = make_course("Add Admin Perm Course")
        make_staff("add_admin_perm_staff@example.com", course)
        client = Client()
        client.login(username="add_admin_perm_staff@example.com", password=PASSWORD)
        response = client.get(reverse('add_admin'))
        self.assertRedirects(response, reverse('staff_home'))


class DeactivateAccountTests(TestCase):
    def setUp(self):
        self.admin = make_admin("deactivate_admin@example.com")
        self.client.login(username="deactivate_admin@example.com", password=PASSWORD)
        self.course = make_course("Deactivate Course")
        self.session = make_session()

    def test_deactivated_staff_cannot_authenticate(self):
        staff = make_staff("deactivate_staff@example.com", self.course)
        self.assertIsNotNone(authenticate(username="deactivate_staff@example.com", password=PASSWORD))
        self.client.get(reverse('toggle_staff_status', args=[staff.id]))
        staff.admin.refresh_from_db()
        self.assertFalse(staff.admin.is_active)
        self.assertIsNone(authenticate(username="deactivate_staff@example.com", password=PASSWORD))

    def test_reactivating_staff_restores_login(self):
        staff = make_staff("reactivate_staff@example.com", self.course)
        self.client.get(reverse('toggle_staff_status', args=[staff.id]))  # deactivate
        self.client.get(reverse('toggle_staff_status', args=[staff.id]))  # reactivate
        staff.admin.refresh_from_db()
        self.assertTrue(staff.admin.is_active)
        self.assertIsNotNone(authenticate(username="reactivate_staff@example.com", password=PASSWORD))

    def test_deactivated_student_cannot_authenticate(self):
        student = make_student("deactivate_student@example.com", self.course, self.session)
        self.client.get(reverse('toggle_student_status', args=[student.id]))
        self.assertIsNone(authenticate(username="deactivate_student@example.com", password=PASSWORD))

    def test_admin_cannot_deactivate_self(self):
        response = self.client.get(reverse('toggle_admin_status', args=[self.admin.admin.id]))
        self.assertRedirects(response, reverse('manage_admin'))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_admin_can_deactivate_another_admin(self):
        other_admin = make_admin("other_admin@example.com")
        response = self.client.get(reverse('toggle_admin_status', args=[other_admin.admin.id]))
        self.assertRedirects(response, reverse('manage_admin'))
        other_admin.refresh_from_db()
        self.assertFalse(other_admin.is_active)
        self.assertIsNone(authenticate(username="other_admin@example.com", password=PASSWORD))


class ManageAdminPageTests(TestCase):
    def test_lists_all_admins(self):
        make_admin("manage_admin_viewer@example.com")
        self.client.login(username="manage_admin_viewer@example.com", password=PASSWORD)
        response = self.client.get(reverse('manage_admin'))
        self.assertEqual(response.status_code, 200)
        self.assertIn("manage_admin_viewer@example.com", response.content.decode())


class AuditLogTests(TestCase):
    def setUp(self):
        make_admin("audit_admin@example.com")
        self.client.login(username="audit_admin@example.com", password=PASSWORD)

    def test_creating_course_is_logged(self):
        self.client.post(reverse('add_course'), {'name': 'Audited Course'})
        log = AuditLog.objects.filter(action='created', target_model='Class').latest('created_at')
        self.assertIn('Audited Course', log.target_repr)
        self.assertEqual(log.actor.email, "audit_admin@example.com")

    def test_deleting_course_is_logged(self):
        course = make_course("To Be Deleted")
        self.client.get(reverse('delete_course', args=[course.id]))
        self.assertTrue(AuditLog.objects.filter(action='deleted', target_model='Class', target_repr='To Be Deleted').exists())

    def test_toggling_staff_status_is_logged(self):
        course = make_course("Audit Toggle Course")
        staff = make_staff("audit_toggle_staff@example.com", course)
        self.client.get(reverse('toggle_staff_status', args=[staff.id]))
        self.assertTrue(AuditLog.objects.filter(action='deactivated', target_model='Staff').exists())

    def test_activity_log_page_shows_entries(self):
        self.client.post(reverse('add_course'), {'name': 'Visible In Log'})
        response = self.client.get(reverse('report_activity_log'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Visible In Log', response.content.decode())

    def test_activity_log_csv_export(self):
        self.client.post(reverse('add_course'), {'name': 'CSV Logged Course'})
        response = self.client.get(reverse('report_activity_log_csv'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('CSV Logged Course', response.content.decode())


class ChronicAbsenceFlaggingTests(TestCase):
    def setUp(self):
        make_admin("chronic_admin@example.com")
        self.client.login(username="chronic_admin@example.com", password=PASSWORD)
        self.course = make_course("Chronic Course")
        self.session = make_session()
        staff = make_staff("chronic_teacher@example.com", self.course)
        self.subject = Subject.objects.create(name="Chronic Subject", staff=staff, course=self.course)

        self.good_student = make_student("good_attendance@example.com", self.course, self.session)
        self.bad_student = make_student("bad_attendance@example.com", self.course, self.session)

        # Good student: 4 present, 0 absent = 100%
        for day in range(4):
            attendance = Attendance.objects.create(
                session=self.session, subject=self.subject, date=datetime.date(2024, 6, 1 + day))
            AttendanceReport.objects.create(student=self.good_student, attendance=attendance, status=True)

        # Bad student: 1 present, 3 absent = 25%
        for day in range(4):
            attendance = Attendance.objects.get_or_create(
                session=self.session, subject=self.subject, date=datetime.date(2024, 6, 1 + day))[0]
            AttendanceReport.objects.create(student=self.bad_student, attendance=attendance, status=(day == 0))

    def test_low_attendance_student_is_flagged(self):
        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        by_name = {row['name']: row for row in response.context['student_summary']}
        bad_row = by_name[str(self.bad_student)]
        good_row = by_name[str(self.good_student)]
        self.assertTrue(bad_row['flagged'])
        self.assertEqual(bad_row['percent'], 25.0)
        self.assertFalse(good_row['flagged'])
        self.assertEqual(good_row['percent'], 100.0)

    def test_worst_attendance_sorted_first(self):
        response = self.client.get(reverse('report_attendance_summary'), {
            'course': self.course.id, 'start_date': '2024-06-01', 'end_date': '2024-06-30',
        })
        names_in_order = [row['name'] for row in response.context['student_summary']]
        self.assertEqual(names_in_order[0], str(self.bad_student))


class PrivacyNoticeTests(TestCase):
    def test_accessible_without_login(self):
        response = self.client.get(reverse('privacy_notice'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Privacy Notice', response.content)


class EditUserCredentialsTests(TestCase):
    """Regression tests: edit_staff/edit_student used to always fail
    validation (the form was never bound to request.FILES, so the
    required profile_pic field was always empty) and, on top of that,
    silently swallowed the failure with no HttpResponse returned - so an
    admin changing a user's email+password believed it saved while
    nothing was actually persisted, locking that user out."""

    def setUp(self):
        make_admin("creds_admin@example.com")
        self.client.login(username="creds_admin@example.com", password=PASSWORD)
        self.course = make_course("Creds Course")
        self.session = make_session()

    def test_edit_staff_email_and_password_without_new_photo(self):
        staff = make_staff("old_staff@example.com", self.course)
        response = self.client.post(reverse('edit_staff', args=[staff.id]), {
            'first_name': 'New', 'last_name': 'Name',
            'email': 'new_staff@example.com', 'gender': 'M', 'address': '1 Test St',
            'password': 'BrandNewPass1',
        })
        self.assertEqual(response.status_code, 302)
        staff.admin.refresh_from_db()
        self.assertEqual(staff.admin.email, 'new_staff@example.com')
        self.assertIsNotNone(authenticate(username='new_staff@example.com', password='BrandNewPass1'))

    def test_edit_student_email_and_password_without_new_photo(self):
        student = make_student("old_student@example.com", self.course, self.session)
        response = self.client.post(reverse('edit_student', args=[student.id]), {
            'first_name': 'New', 'last_name': 'Name',
            'email': 'new_student@example.com', 'gender': 'F', 'address': '1 Test St',
            'password': 'BrandNewPass1',
            'course': self.course.id, 'session': self.session.id,
        })
        self.assertEqual(response.status_code, 302)
        student.admin.refresh_from_db()
        self.assertEqual(student.admin.email, 'new_student@example.com')
        self.assertIsNotNone(authenticate(username='new_student@example.com', password='BrandNewPass1'))

    def test_edit_staff_invalid_submission_rerenders_instead_of_crashing(self):
        staff = make_staff("invalid_staff@example.com", self.course)
        response = self.client.post(reverse('edit_staff', args=[staff.id]), {
            'first_name': '', 'last_name': 'Name',
            'email': 'invalid_staff@example.com', 'gender': 'M', 'address': '1 Test St',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'fill', response.content.lower())

    def test_edit_admin_email_and_password_without_new_photo(self):
        target = make_admin("old_admin@example.com")
        response = self.client.post(reverse('edit_admin', args=[target.admin.id]), {
            'first_name': 'New', 'last_name': 'Name',
            'email': 'new_admin@example.com', 'gender': 'M', 'address': '1 Test St',
            'password': 'BrandNewPass1',
        })
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.email, 'new_admin@example.com')
        self.assertIsNotNone(authenticate(username='new_admin@example.com', password='BrandNewPass1'))

    def test_edit_admin_self_password_change_keeps_session_valid(self):
        actor = CustomUser.objects.get(email="creds_admin@example.com")
        response = self.client.post(reverse('edit_admin', args=[actor.admin.id]), {
            'first_name': 'Creds', 'last_name': 'Admin',
            'email': 'creds_admin@example.com', 'gender': 'M', 'address': '1 Test St',
            'password': 'BrandNewPass1',
        })
        self.assertEqual(response.status_code, 302)
        # A stale session-auth-hash would bounce this authenticated request
        # to the login page instead of serving the page.
        response = self.client.get(reverse('manage_admin'))
        self.assertEqual(response.status_code, 200)

    def test_delete_admin(self):
        target = make_admin("deleteme_admin@example.com")
        response = self.client.get(reverse('delete_admin', args=[target.admin.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustomUser.objects.filter(email="deleteme_admin@example.com").exists())

    def test_cannot_delete_own_admin_account(self):
        actor = CustomUser.objects.get(email="creds_admin@example.com")
        response = self.client.get(reverse('delete_admin', args=[actor.admin.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustomUser.objects.filter(email="creds_admin@example.com").exists())


class ParentFeatureTests(TestCase):
    def setUp(self):
        cache.clear()  # rate_limited()'s counters live outside the DB, so the per-test rollback doesn't reset them
        self.admin_user = make_admin("parent_feature_admin@example.com")
        self.course = make_course("Parent Feature Course")
        self.session = make_session()
        self.student = make_student("parent_feature_kid@example.com", self.course, self.session)

    def _register_parent(self, email="new_parent@example.com", students=None):
        students = students or [self.student]
        data = {
            'first_name': 'New', 'last_name': 'Parent', 'email': email,
            'gender': 'M', 'address': '1 Test St', 'password': PASSWORD,
            'contact_number': '5551234',
            'child-TOTAL_FORMS': str(len(students)), 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
        }
        for i, student in enumerate(students):
            data[f'child-{i}-course'] = self.course.id
            data[f'child-{i}-student'] = student.id
            data[f'child-{i}-relationship'] = 'father'
            data[f'child-{i}-date_of_birth'] = '2015-01-01'
        return self.client.post(reverse('parent_register'), data)

    def test_registration_creates_pending_link_and_active_account(self):
        response = self._register_parent()
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email="new_parent@example.com")
        self.assertEqual(str(user.user_type), '4')
        self.assertTrue(user.is_active)
        link = ParentStudentLink.objects.get(parent=user.parent, student=self.student)
        self.assertEqual(link.status, 0)

    def test_registration_page_get_renders_single_child_form_by_default(self):
        # Regression test: formset_factory's total form count is
        # max(initial_form_count(), min_num) + extra, so extra=1 alongside
        # min_num=1 used to render 2 required child blocks on a fresh GET
        # instead of 1, forcing every parent to fill in a second kid before
        # they could submit at all.
        response = self.client.get(reverse('parent_register'))
        self.assertEqual(len(response.context['link_formset'].forms), 1)

    def test_link_another_kid_page_get_renders_single_child_form_by_default(self):
        self._register_parent()
        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.get(reverse('parent_link_kid'))
        self.assertEqual(len(response.context['formset'].forms), 1)

    def test_registration_supports_multiple_children_in_one_submission(self):
        second_student = make_student("parent_feature_kid2@example.com", self.course, self.session)
        response = self._register_parent(students=[self.student, second_student])
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email="new_parent@example.com")
        self.assertEqual(ParentStudentLink.objects.filter(parent=user.parent).count(), 2)
        self.assertTrue(ParentStudentLink.objects.filter(parent=user.parent, student=self.student).exists())
        self.assertTrue(ParentStudentLink.objects.filter(parent=user.parent, student=second_student).exists())

    def test_registration_requires_at_least_one_child(self):
        response = self.client.post(reverse('parent_register'), {
            'first_name': 'New', 'last_name': 'Parent', 'email': 'no_child_parent@example.com',
            'gender': 'M', 'address': '1 Test St', 'password': PASSWORD, 'contact_number': '5551234',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(email='no_child_parent@example.com').exists())

    def test_registration_rejects_non_numeric_contact_number(self):
        data = {
            'first_name': 'New', 'last_name': 'Parent', 'email': 'bad_contact_parent@example.com',
            'gender': 'M', 'address': '1 Test St', 'password': PASSWORD,
            'contact_number': '555-1234',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        }
        response = self.client.post(reverse('parent_register'), data)
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not redirected
        self.assertFalse(CustomUser.objects.filter(email='bad_contact_parent@example.com').exists())

    def test_edit_parent_rejects_non_numeric_contact_number(self):
        self._register_parent()
        parent = Parent.objects.get(admin__email="new_parent@example.com")
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        response = self.client.post(reverse('edit_parent', args=[parent.id]), {
            'first_name': 'New', 'last_name': 'Parent', 'email': 'new_parent@example.com',
            'gender': 'M', 'address': '1 Test St', 'contact_number': 'not-a-number',
        })
        self.assertEqual(response.status_code, 200)
        parent.refresh_from_db()
        self.assertEqual(parent.contact_number, '5551234')  # unchanged

    def test_link_another_kid_supports_multiple_children_at_once(self):
        second_student = make_student("parent_feature_kid3@example.com", self.course, self.session)
        self._register_parent()
        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.post(reverse('parent_link_kid'), {
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': second_student.id,
            'child-0-relationship': 'mother', 'child-0-date_of_birth': '2016-02-02',
        })
        self.assertEqual(response.status_code, 302)
        parent = Parent.objects.get(admin__email="new_parent@example.com")
        self.assertTrue(ParentStudentLink.objects.filter(parent=parent, student=second_student).exists())

    def test_parent_can_log_in_immediately_after_registering(self):
        self._register_parent()
        self.assertIsNotNone(authenticate(username="new_parent@example.com", password=PASSWORD))

    def test_pending_link_grants_no_attendance_or_leave_access(self):
        self._register_parent()
        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.post(reverse('parent_view_attendance'), {
            'student': self.student.id, 'subject': 1,
            'start_date': '2024-01-01', 'end_date': '2024-01-31',
        })
        self.assertEqual(response.status_code, 404)

    def test_admin_approval_grants_access_and_backfills_dob(self):
        self._register_parent()
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        link = ParentStudentLink.objects.get(student=self.student)
        response = self.client.post(reverse('view_parent_link_requests'), {'id': link.id, 'status': '1'})
        self.assertEqual(response.content, b'True')
        link.refresh_from_db()
        self.assertEqual(link.status, 1)
        self.student.refresh_from_db()
        self.assertEqual(str(self.student.date_of_birth), '2015-01-01')

        self.client.logout()
        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.get(reverse('parent_home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Approved')

    def test_approved_dob_is_not_overwritten_by_a_second_link(self):
        self._register_parent()
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        link = ParentStudentLink.objects.get(student=self.student)
        self.client.post(reverse('view_parent_link_requests'), {'id': link.id, 'status': '1'})
        self.client.logout()

        # A second parent submits a different (mistaken) DOB for the same kid.
        self._register_parent(email="second_parent@example.com")
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        second_link = ParentStudentLink.objects.get(parent__admin__email="second_parent@example.com")
        self.client.post(reverse('view_parent_link_requests'), {'id': second_link.id, 'status': '1'})
        self.student.refresh_from_db()
        self.assertEqual(str(self.student.date_of_birth), '2015-01-01')

    def test_parent_cannot_access_a_student_without_an_approved_link(self):
        other_student = make_student("other_kid@example.com", self.course, self.session)
        self._register_parent()  # links to self.student only
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        link = ParentStudentLink.objects.get(student=self.student)
        self.client.post(reverse('view_parent_link_requests'), {'id': link.id, 'status': '1'})
        self.client.logout()

        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.post(reverse('parent_view_attendance'), {
            'student': other_student.id, 'subject': 1,
            'start_date': '2024-01-01', 'end_date': '2024-01-31',
        })
        self.assertEqual(response.status_code, 404)

    def test_parent_apply_leave_notifies_class_teachers(self):
        teacher = make_staff("parent_feature_teacher@example.com", self.course)
        Subject.objects.create(name="Parent Feature Subject", staff=teacher, course=self.course)
        self._register_parent()
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        link = ParentStudentLink.objects.get(student=self.student)
        self.client.post(reverse('view_parent_link_requests'), {'id': link.id, 'status': '1'})
        self.client.logout()

        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.post(reverse('parent_apply_leave'), {
            'student': self.student.id, 'date': '2024-06-01', 'message': 'Family trip',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(LeaveReportStudent.objects.filter(student=self.student, message='Family trip').exists())
        self.assertTrue(NotificationStaff.objects.filter(staff=teacher).exists())

    def test_middleware_blocks_parent_from_other_role_views(self):
        self._register_parent()
        self.client.login(username="new_parent@example.com", password=PASSWORD)
        response = self.client.get(reverse('manage_student'))
        self.assertRedirects(response, reverse('parent_home'))

    def test_manage_parent_lists_registered_parent(self):
        self._register_parent()
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        response = self.client.get(reverse('manage_parent'))
        self.assertContains(response, 'new_parent@example.com')

    def test_edit_parent_updates_email_and_password(self):
        self._register_parent()
        parent = Parent.objects.get(admin__email="new_parent@example.com")
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        response = self.client.post(reverse('edit_parent', args=[parent.id]), {
            'first_name': 'New', 'last_name': 'Parent', 'email': 'updated_parent@example.com',
            'gender': 'M', 'address': '1 Test St', 'contact_number': '5559999',
            'password': 'BrandNewPass1',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(authenticate(username='updated_parent@example.com', password='BrandNewPass1'))

    def test_delete_parent(self):
        self._register_parent()
        parent = Parent.objects.get(admin__email="new_parent@example.com")
        self.client.login(username="parent_feature_admin@example.com", password=PASSWORD)
        response = self.client.get(reverse('delete_parent', args=[parent.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustomUser.objects.filter(email="new_parent@example.com").exists())


class ParentNotificationTests(TestCase):
    """Regression coverage for the notification gaps: link decisions and
    leave decisions used to only ever email a parent (nothing showed up
    in their in-app inbox), and there was no way for an HOD to broadcast
    a notification to parents the way they already could to staff/students."""

    def setUp(self):
        cache.clear()  # rate_limited()'s counters live outside the DB, so the per-test rollback doesn't reset them
        self.admin_user = make_admin("notif_admin@example.com")
        self.course = make_course("Notif Course")
        self.session = make_session()
        self.student = make_student("notif_kid@example.com", self.course, self.session)
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('parent_register'), {
            'first_name': 'Notif', 'last_name': 'Parent', 'email': 'notif_parent@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })
        self.client.logout()
        self.parent = Parent.objects.get(admin__email='notif_parent@example.com')
        self.link = ParentStudentLink.objects.get(parent=self.parent, student=self.student)

    def test_link_approval_creates_in_app_notification(self):
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_parent_link_requests'), {'id': self.link.id, 'status': '1'})
        self.assertTrue(NotificationParent.objects.filter(parent=self.parent, is_read=False).exists())

    def test_link_rejection_creates_in_app_notification(self):
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_parent_link_requests'), {'id': self.link.id, 'status': '-1'})
        note = NotificationParent.objects.get(parent=self.parent)
        self.assertIn('rejected', note.message)

    def test_parent_notification_page_marks_read(self):
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_parent_link_requests'), {'id': self.link.id, 'status': '1'})
        self.client.logout()

        self.client.login(username="notif_parent@example.com", password=PASSWORD)
        response = self.client.get(reverse('parent_view_notification'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'approved')
        self.assertFalse(NotificationParent.objects.filter(parent=self.parent, is_read=False).exists())

    def test_leave_decided_by_admin_notifies_applying_parent(self):
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_parent_link_requests'), {'id': self.link.id, 'status': '1'})
        self.client.logout()

        self.client.login(username="notif_parent@example.com", password=PASSWORD)
        self.client.post(reverse('parent_apply_leave'), {
            'student': self.student.id, 'date': '2024-06-01', 'message': 'Family trip',
        })
        leave = LeaveReportStudent.objects.get(student=self.student)
        self.assertEqual(leave.applied_by_parent, self.parent)
        self.client.logout()

        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_student_leave'), {'id': leave.id, 'status': '1'})
        self.assertTrue(NotificationParent.objects.filter(parent=self.parent).exists())
        self.assertTrue(NotificationStudent.objects.filter(student=self.student).exists())

    def test_leave_decided_by_staff_notifies_applying_parent(self):
        teacher = make_staff("notif_teacher@example.com", self.course)
        Subject.objects.create(name="Notif Subject", staff=teacher, course=self.course)

        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        self.client.post(reverse('view_parent_link_requests'), {'id': self.link.id, 'status': '1'})
        self.client.logout()

        self.client.login(username="notif_parent@example.com", password=PASSWORD)
        self.client.post(reverse('parent_apply_leave'), {
            'student': self.student.id, 'date': '2024-06-01', 'message': 'Family trip',
        })
        leave = LeaveReportStudent.objects.get(student=self.student)
        self.client.logout()

        self.client.login(username="notif_teacher@example.com", password=PASSWORD)
        response = self.client.post(reverse('staff_view_student_leave'), {'id': leave.id, 'status': '1'})
        self.assertEqual(response.content, b'True')
        self.assertTrue(NotificationParent.objects.filter(parent=self.parent).exists())

    def test_admin_notify_parent_broadcast(self):
        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        response = self.client.get(reverse('admin_notify_parent'))
        self.assertContains(response, 'notif_parent@example.com')

        response = self.client.post(reverse('send_parent_notification'), {
            'id': self.parent.admin.id, 'message': 'Reminder: PTA meeting Friday',
        })
        self.assertEqual(response.content, b'True')
        self.assertTrue(NotificationParent.objects.filter(
            parent=self.parent, message='Reminder: PTA meeting Friday').exists())

    def test_direct_student_leave_without_parent_is_unaffected(self):
        """A student applying for their own leave (no parent involved)
        must not error out or create a stray NotificationParent."""
        student_user = self.student.admin
        self.client.login(username=student_user.email, password=PASSWORD)
        self.client.post(reverse('student_apply_leave'), {'date': '2024-07-01', 'message': 'Sick'})
        leave = LeaveReportStudent.objects.get(student=self.student, date='2024-07-01')
        self.assertIsNone(leave.applied_by_parent)
        self.client.logout()

        self.client.login(username="notif_admin@example.com", password=PASSWORD)
        response = self.client.post(reverse('view_student_leave'), {'id': leave.id, 'status': '1'})
        self.assertEqual(response.content, b'True')


class SecurityHardeningTests(TestCase):
    """Regression coverage for the full security/code review pass:
    CSV formula injection, weak passwords, implausible dates, the
    get_attendance access-control gap, and rate limiting."""

    def setUp(self):
        cache.clear()  # rate_limited()'s counters live outside the DB
        self.admin_user = make_admin("hardening_admin@example.com")
        self.course = make_course("Hardening Course")
        self.session = make_session()
        self.student = make_student("hardening_kid@example.com", self.course, self.session)

    def test_csv_export_neutralizes_formula_injection(self):
        LeaveReportStudent.objects.create(
            student=self.student, date="2024-06-01", message="=cmd|'/c calc'!A0", status=0)
        self.client.login(username="hardening_admin@example.com", password=PASSWORD)
        response = self.client.get(reverse('report_leave_csv'))
        content = response.content.decode()
        self.assertIn("'=cmd|'/c calc'!A0", content)
        self.assertNotIn('\n=cmd', content)  # never a bare formula at the start of a cell

    def test_registration_rejects_common_password(self):
        response = self.client.post(reverse('parent_register'), {
            'first_name': 'Weak', 'last_name': 'Parent', 'email': 'weak_pw_parent@example.com',
            'gender': 'M', 'address': 'addr', 'password': 'password123', 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not redirected
        self.assertFalse(CustomUser.objects.filter(email='weak_pw_parent@example.com').exists())

    def test_registration_rejects_future_date_of_birth(self):
        response = self.client.post(reverse('parent_register'), {
            'first_name': 'Future', 'last_name': 'Parent', 'email': 'future_dob_parent@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2099-01-01',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(email='future_dob_parent@example.com').exists())

    def test_get_attendance_rejects_student(self):
        Subject.objects.create(name="Hardening Subject", course=self.course,
                                staff=make_staff("hardening_teacher@example.com", self.course))
        self.client.login(username=self.student.admin.email, password=PASSWORD)
        response = self.client.post(reverse('get_attendance'), {
            'subject': 1, 'session': self.session.id,
        })
        self.assertEqual(response.status_code, 403)

    def test_get_students_for_registration_excludes_inactive_students(self):
        inactive = make_student("hardening_inactive_kid@example.com", self.course, self.session)
        inactive.admin.is_active = False
        inactive.admin.save()
        response = self.client.post(reverse('parent_register_get_students'), {'course': self.course.id})
        names = [s['name'] for s in json.loads(response.json())]
        self.assertNotIn(inactive.admin.last_name + " " + inactive.admin.first_name, names)
        self.assertIn(self.student.admin.last_name + " " + self.student.admin.first_name, names)

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.client.post(reverse('user_login'), {'email': 'nobody@example.com', 'password': 'wrong'})
        response = self.client.post(
            reverse('user_login'), {'email': self.admin_user.email, 'password': PASSWORD}, follow=True)
        self.assertContains(response, "Too many login attempts")

    def test_editing_parent_without_a_new_photo_does_not_crash(self):
        """Regression: clean_profile_pic crashed on the existing-photo-URL
        string Django substitutes in when no new file is uploaded on edit."""
        self.client.login(username="hardening_admin@example.com", password=PASSWORD)
        self.client.post(reverse('parent_register'), {
            'first_name': 'Photo', 'last_name': 'Parent', 'email': 'photo_parent@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })
        parent = Parent.objects.get(admin__email='photo_parent@example.com')
        response = self.client.post(reverse('edit_parent', args=[parent.id]), {
            'first_name': 'Photo', 'last_name': 'Parent', 'email': 'photo_parent@example.com',
            'gender': 'M', 'address': 'addr', 'contact_number': '555',
        })
        self.assertEqual(response.status_code, 302)


class UXImprovementTests(TestCase):
    """Regression coverage for the product/UX pass: global search and
    flagging contested (same-student, multiple-pending-parent) link
    requests in the approval queue."""

    def setUp(self):
        self.admin_user = make_admin("ux_admin@example.com")
        self.course = make_course("UX Course")
        self.session = make_session()
        self.student = make_student("findme_kid@example.com", self.course, self.session)
        self.client.login(username="ux_admin@example.com", password=PASSWORD)

    def test_global_search_finds_matching_student(self):
        response = self.client.get(reverse('global_search'), {'q': 'findme'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'findme_kid@example.com')

    def test_global_search_no_query_shows_no_results(self):
        response = self.client.get(reverse('global_search'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['results']), 0)

    def test_global_search_is_admin_only(self):
        make_staff("ux_search_staff@example.com", self.course)
        self.client.logout()
        self.client.login(username="ux_search_staff@example.com", password=PASSWORD)
        response = self.client.get(reverse('global_search'), {'q': 'findme'})
        self.assertRedirects(response, reverse('staff_home'))

    def test_contested_link_requests_are_flagged(self):
        # Two different parents both request the same student - the admin
        # should see this flagged, not have to notice it by eye.
        self.client.post(reverse('parent_register'), {
            'first_name': 'A', 'last_name': 'Parent', 'email': 'contested_a@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })
        self.client.post(reverse('parent_register'), {
            'first_name': 'B', 'last_name': 'Parent', 'email': 'contested_b@example.com',
            'gender': 'F', 'address': 'addr', 'password': PASSWORD, 'contact_number': '556',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'mother', 'child-0-date_of_birth': '2015-06-01',
        })
        response = self.client.get(reverse('view_parent_link_requests'))
        self.assertContains(response, 'Also requested by another parent')

    def test_uncontested_link_request_is_not_flagged(self):
        self.client.post(reverse('parent_register'), {
            'first_name': 'Solo', 'last_name': 'Parent', 'email': 'solo_parent@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })
        response = self.client.get(reverse('view_parent_link_requests'))
        self.assertNotContains(response, 'Also requested by another parent')
        self.assertFalse(NotificationParent.objects.exists())


class ParentLinkRequestCapTests(TestCase):
    """A student can only have MAX_PENDING_LINKS_PER_STUDENT pending
    requests at once, regardless of which parent is asking - otherwise
    nothing bounds how many fake accounts can pile pending requests onto
    the same kid."""

    def setUp(self):
        cache.clear()  # rate_limited()'s counters live outside the DB, so the per-test rollback doesn't reset them
        self.admin_user = make_admin("cap_admin@example.com")
        self.course = make_course("Cap Course")
        self.session = make_session()
        self.student = make_student("cap_kid@example.com", self.course, self.session)

    def _register(self, email):
        return self.client.post(reverse('parent_register'), {
            'first_name': 'P', 'last_name': 'Arent', 'email': email,
            'gender': 'M', 'address': 'addr', 'password': PASSWORD, 'contact_number': '555',
            'child-TOTAL_FORMS': '1', 'child-INITIAL_FORMS': '0',
            'child-MIN_NUM_FORMS': '0', 'child-MAX_NUM_FORMS': '1000',
            'child-0-course': self.course.id, 'child-0-student': self.student.id,
            'child-0-relationship': 'father', 'child-0-date_of_birth': '2015-01-01',
        })

    def test_pending_requests_capped_per_student(self):
        for i in range(3):
            response = self._register(f'cap_parent_{i}@example.com')
            self.assertEqual(response.status_code, 302, f"request {i} should have been accepted")
        self.assertEqual(ParentStudentLink.objects.filter(student=self.student, status=0).count(), 3)

        # A 4th pending request for the same student should be rejected.
        response = self._register('cap_parent_overflow@example.com')
        self.assertEqual(response.status_code, 200)  # re-rendered with a validation error
        self.assertFalse(CustomUser.objects.filter(email='cap_parent_overflow@example.com').exists())
        self.assertEqual(ParentStudentLink.objects.filter(student=self.student, status=0).count(), 3)

    def test_approving_one_frees_up_capacity(self):
        for i in range(3):
            self._register(f'cap_parent_{i}@example.com')
        self.client.login(username="cap_admin@example.com", password=PASSWORD)
        link = ParentStudentLink.objects.filter(student=self.student, status=0).first()
        self.client.post(reverse('view_parent_link_requests'), {'id': link.id, 'status': '1'})
        self.client.logout()

        # Only 2 pending remain now, so a new request should succeed.
        response = self._register('cap_parent_after_approval@example.com')
        self.assertEqual(response.status_code, 302)


class DjangoAdminCompatibilityTests(TestCase):
    """Regression test: Django's own /admin/ "Add user"/"Change user"
    pages 500'd (FieldError: Unknown field(s) (username)) because
    CustomUser has no username field and USER_TYPE's choice keys didn't
    match the CharField's actual (string) values."""

    def setUp(self):
        self.superuser = CustomUser.objects.create_superuser(
            email="dj_admin_super@example.com", password=PASSWORD, user_type=1,
            first_name="Super", last_name="User")
        self.client.login(username="dj_admin_super@example.com", password=PASSWORD)

    def test_add_user_page_loads(self):
        response = self.client.get('/admin/main_app/customuser/add/')
        self.assertEqual(response.status_code, 200)

    def test_add_user_via_admin_succeeds(self):
        response = self.client.post('/admin/main_app/customuser/add/', {
            'email': 'dj_admin_created@example.com',
            'first_name': 'DJ', 'last_name': 'Created',
            'user_type': '1', 'gender': 'M', 'address': 'addr',
            'password1': 'Xk7pQr29Zt!8', 'password2': 'Xk7pQr29Zt!8',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustomUser.objects.filter(email='dj_admin_created@example.com').exists())

    def test_change_user_page_loads_and_saves(self):
        response = self.client.get(f'/admin/main_app/customuser/{self.superuser.id}/change/')
        self.assertEqual(response.status_code, 200)


class MultiRoleAccountTests(TestCase):
    """Same email can now hold more than one role (e.g. Staff and Parent),
    attached only by an Admin from the Manage screens - see
    utils.user_roles/grant_role and the existing_user branches in
    hod_views.add_admin/add_staff/add_student/grant_parent_role."""

    def setUp(self):
        cache.clear()
        self.admin_user = make_admin("multirole_admin@example.com")
        self.client.login(username="multirole_admin@example.com", password=PASSWORD)
        self.course = make_course("Multirole Course")
        self.session = make_session()

    def test_add_staff_attaches_role_to_existing_email_without_touching_password(self):
        parent = make_parent("shared_email@example.com")
        old_password_hash = parent.admin.password
        response = self.client.post(reverse('add_staff'), {
            'first_name': 'New', 'last_name': 'Name', 'email': 'shared_email@example.com',
            'gender': 'M', 'address': 'addr',
        })
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='shared_email@example.com')
        self.assertTrue(hasattr(user, 'staff'))
        self.assertTrue(hasattr(user, 'parent'))  # untouched
        self.assertEqual(user.password, old_password_hash)  # untouched
        self.assertEqual(CustomUser.objects.filter(email='shared_email@example.com').count(), 1)

    def test_add_staff_rejects_email_that_already_has_that_role(self):
        make_staff("already_staff@example.com", self.course)
        response = self.client.post(reverse('add_staff'), {
            'first_name': 'New', 'last_name': 'Name', 'email': 'already_staff@example.com',
            'gender': 'M', 'address': 'addr',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertContains(response, 'already a Staff member')

    def test_add_staff_still_creates_fresh_account_for_new_email(self):
        response = self.client.post(reverse('add_staff'), {
            'first_name': 'Brand', 'last_name': 'New', 'email': 'brand_new_staff@example.com',
            'gender': 'M', 'address': 'addr', 'password': PASSWORD,
            'profile_pic': make_image_file(),
        })
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='brand_new_staff@example.com')
        self.assertEqual(user_roles(user), [('2', 'Staff')])

    def test_add_student_attaches_role_with_course_and_session(self):
        staff = make_staff("future_student@example.com", self.course)
        response = self.client.post(reverse('add_student'), {
            'first_name': 'New', 'last_name': 'Name', 'email': 'future_student@example.com',
            'gender': 'M', 'address': 'addr', 'course': self.course.id, 'session': self.session.id,
        })
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='future_student@example.com')
        self.assertTrue(hasattr(user, 'student'))
        self.assertEqual(user.student.course, self.course)
        self.assertTrue(hasattr(user, 'staff'))  # untouched

    def test_grant_parent_role_attaches_to_existing_account(self):
        make_staff("future_parent@example.com", self.course)
        response = self.client.post(reverse('grant_parent_role'), {
            'email': 'future_parent@example.com', 'contact_number': '5551234',
        })
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='future_parent@example.com')
        self.assertTrue(hasattr(user, 'parent'))
        self.assertTrue(hasattr(user, 'staff'))

    def test_grant_parent_role_rejects_unknown_email(self):
        response = self.client.post(reverse('grant_parent_role'), {
            'email': 'nobody_here@example.com', 'contact_number': '5551234',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No existing account')
        self.assertFalse(CustomUser.objects.filter(email='nobody_here@example.com').exists())

    def test_grant_parent_role_rejects_non_numeric_contact_number(self):
        make_staff("bad_contact_future_parent@example.com", self.course)
        response = self.client.post(reverse('grant_parent_role'), {
            'email': 'bad_contact_future_parent@example.com', 'contact_number': '555-1234',
        })
        self.assertEqual(response.status_code, 200)
        user = CustomUser.objects.get(email='bad_contact_future_parent@example.com')
        self.assertFalse(hasattr(user, 'parent'))

    def test_delete_staff_on_multi_role_account_keeps_other_role(self):
        staff = make_staff("keep_parent@example.com", self.course)
        grant_role(staff.admin, '4', contact_number='5551234')
        response = self.client.get(reverse('delete_staff', args=[staff.id]))
        self.assertEqual(response.status_code, 302)
        user = CustomUser.objects.get(email='keep_parent@example.com')
        self.assertFalse(hasattr(user, 'staff'))
        self.assertTrue(hasattr(user, 'parent'))

    def test_delete_staff_on_single_role_account_removes_whole_account(self):
        staff = make_staff("only_role@example.com", self.course)
        response = self.client.get(reverse('delete_staff', args=[staff.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustomUser.objects.filter(email='only_role@example.com').exists())

    def test_manage_staff_lists_user_whose_primary_role_is_admin(self):
        admin_extra = make_admin("mgmt_admin_extra@example.com")
        grant_role(admin_extra, '2')  # also Staff now, but user_type stays '1'
        response = self.client.get(reverse('manage_staff'))
        self.assertContains(response, 'mgmt_admin_extra@example.com')


class MultiRoleLoginTests(TestCase):
    """Login/session behavior for accounts with more than one role - see
    views.doLogin/choose_role and middleware.LoginCheckMiddleWare."""

    def setUp(self):
        cache.clear()
        self.course = make_course()
        self.session = make_session()

    def test_single_role_login_skips_role_picker(self):
        make_staff("solo_staff@example.com", self.course)
        response = self.client.post(reverse('user_login'), {
            'email': 'solo_staff@example.com', 'password': PASSWORD,
        })
        self.assertRedirects(response, reverse('staff_home'))
        self.assertEqual(self.client.session['active_role'], '2')

    def test_multi_role_login_lands_on_choose_role(self):
        staff = make_staff("multi_login@example.com", self.course)
        grant_role(staff.admin, '4', contact_number='5551234')
        # Don't let assertRedirects follow the redirect - that issues a
        # second request to choose_role, which the middleware's self-heal
        # would populate active_role for, defeating the point of this check.
        response = self.client.post(reverse('user_login'), {
            'email': 'multi_login@example.com', 'password': PASSWORD,
        })
        self.assertRedirects(response, reverse('choose_role'), fetch_redirect_response=False)
        self.assertNotIn('active_role', self.client.session)

    def test_choose_role_sets_session_and_redirects(self):
        staff = make_staff("choose_flow@example.com", self.course)
        grant_role(staff.admin, '4', contact_number='5551234')
        self.client.post(reverse('user_login'), {
            'email': 'choose_flow@example.com', 'password': PASSWORD,
        })
        response = self.client.post(reverse('choose_role'), {'role': '4'})
        self.assertRedirects(response, reverse('parent_home'))
        self.assertEqual(self.client.session['active_role'], '4')

    def test_choose_role_rejects_role_the_user_doesnt_have(self):
        # Needs a genuinely multi-role account - a single-role account
        # short-circuits straight through choose_role instead of ever
        # rendering the picker, so it wouldn't exercise this guard at all.
        staff = make_staff("guard_flow@example.com", self.course)
        grant_role(staff.admin, '4', contact_number='5551234')
        self.client.post(reverse('user_login'), {
            'email': 'guard_flow@example.com', 'password': PASSWORD,
        })
        response = self.client.post(reverse('choose_role'), {'role': '1'}, follow=True)
        self.assertContains(response, 'Invalid role selection')

    def test_active_role_gates_access_even_when_user_also_has_other_role(self):
        staff = make_staff("gate_flow@example.com", self.course)
        grant_role(staff.admin, '1')  # also an Admin
        self.client.post(reverse('user_login'), {
            'email': 'gate_flow@example.com', 'password': PASSWORD,
        })
        self.client.post(reverse('choose_role'), {'role': '2'})  # continue as Staff
        # hod_views is blocked while active_role is Staff, even though this
        # account also technically has the Admin role - "one dashboard at
        # a time" has to actually hold, not just be true at login.
        response = self.client.get(reverse('admin_home'))
        self.assertRedirects(response, reverse('staff_home'))

    def test_stale_session_self_heals_to_a_valid_role(self):
        # self.client.login() bypasses doLogin entirely, so it never sets
        # active_role - this is exactly the "pre-existing session" case the
        # middleware's self-heal path exists for (also lets every other
        # test class in this suite keep using self.client.login()
        # unchanged even though multi-role accounts now exist).
        make_staff("selfheal@example.com", self.course)
        self.client.login(username="selfheal@example.com", password=PASSWORD)
        response = self.client.get(reverse('staff_home'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['active_role'], '2')
