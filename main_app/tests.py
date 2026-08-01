import datetime
import json

from django.test import Client, TestCase
from django.urls import reverse

from .models import (Attendance, AttendanceReport, Course, CustomUser,
                     Session, Staff, Student, StudentResult, Subject)

PASSWORD = "pass1234"


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
