import datetime
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from .models import (Attendance, AttendanceReport, Course, CustomUser,
                     Session, Staff, Student, StudentResult, Subject)

PASSWORD = "pass1234"


def make_admin(email):
    return CustomUser.objects.create_superuser(
        email=email, password=PASSWORD, user_type=1,
        first_name="Bulk", last_name="Admin",
    )


def csv_file(content, name="upload.csv"):
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


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
        content = "name,staff_email,course\nQuran,bulk_teacher@example.com,Bulk Subject Course\n"
        self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertTrue(Subject.objects.filter(name="Quran", course=self.course, staff=self.staff).exists())

    def test_unknown_staff_email_reported_as_error(self):
        content = "name,staff_email,course\nQuran,missing@example.com,Bulk Subject Course\n"
        response = self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(Subject.objects.filter(name="Quran").exists())

    def test_unknown_course_reported_as_error(self):
        content = "name,staff_email,course\nQuran,bulk_teacher@example.com,No Such Course\n"
        response = self.client.post(reverse('bulk_upload_subjects'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')


class BulkUploadStaffTests(TestCase):
    def setUp(self):
        make_admin("bulk_admin_staff@example.com")
        self.client.login(username="bulk_admin_staff@example.com", password=PASSWORD)
        self.course = make_course("Staff Bulk Course")

    def test_creates_staff_account_with_working_generated_password(self):
        content = ("first_name,last_name,email,gender,address,course\n"
                   "New,Teacher,new_teacher@example.com,F,1 Test St,Staff Bulk Course\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        result = response.context['results'][0]
        self.assertEqual(result['status'], 'success')
        user = CustomUser.objects.get(email="new_teacher@example.com")
        self.assertEqual(user.user_type, '2')
        self.assertEqual(user.staff.course, self.course)
        generated_password = result['message'].split(': ')[-1]
        self.assertTrue(user.check_password(generated_password))

    def test_invalid_gender_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address,course\n"
                   "Bad,Gender,bad_gender@example.com,X,addr,Staff Bulk Course\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(CustomUser.objects.filter(email="bad_gender@example.com").exists())

    def test_unknown_course_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address,course\n"
                   "No,Course,no_course@example.com,M,addr,Nonexistent Course\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')

    def test_duplicate_email_skipped(self):
        make_staff("dup_staff@example.com", self.course)
        content = ("first_name,last_name,email,gender,address,course\n"
                   "Dup,Staff,dup_staff@example.com,M,addr,Staff Bulk Course\n")
        response = self.client.post(reverse('bulk_upload_staff'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'skipped')


class BulkUploadStudentTests(TestCase):
    def setUp(self):
        make_admin("bulk_admin_student@example.com")
        self.client.login(username="bulk_admin_student@example.com", password=PASSWORD)
        self.course = make_course("Student Bulk Course")
        self.session = make_session()

    def test_creates_student_account(self):
        content = ("first_name,last_name,email,gender,address,course,session_start_year,session_end_year\n"
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
        content = ("first_name,last_name,email,gender,address,course,session_start_year,session_end_year\n"
                   "New,Student,no_session@example.com,M,1 Test St,Student Bulk Course,1999-01-01,1999-12-31\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')
        self.assertFalse(CustomUser.objects.filter(email="no_session@example.com").exists())

    def test_bad_date_format_reported_as_error(self):
        content = ("first_name,last_name,email,gender,address,course,session_start_year,session_end_year\n"
                   "Bad,Date,bad_date@example.com,M,1 Test St,Student Bulk Course,not-a-date,also-not\n")
        response = self.client.post(reverse('bulk_upload_students'), {'csv_file': csv_file(content)})
        self.assertEqual(response.context['results'][0]['status'], 'error')

    def test_duplicate_email_skipped(self):
        make_student("dup_student@example.com", self.course, self.session)
        content = ("first_name,last_name,email,gender,address,course,session_start_year,session_end_year\n"
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
