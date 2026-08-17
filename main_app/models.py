from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import UserManager
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.db import models
from django.contrib.auth.models import AbstractUser




class CustomUserManager(UserManager):
    def _create_user(self, email, password, **extra_fields):
        email = self.normalize_email(email)
        user = CustomUser(email=email, **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        assert extra_fields["is_staff"]
        assert extra_fields["is_superuser"]
        return self._create_user(email, password, **extra_fields)


class Session(models.Model):
    start_year = models.DateField()
    end_year = models.DateField()

    def __str__(self):
        return "From " + str(self.start_year) + " to " + str(self.end_year)


class CustomUser(AbstractUser):
    # Keys are strings, not ints: user_type is a CharField, and every
    # comparison across the app (middleware, views, templates) already
    # treats it as one ('1', '2', ...). Int keys here only ever caused
    # main_app.models.Field.validate() to reject a freshly-created user
    # ('1' != 1) the moment something ran real ModelForm validation
    # against it - which none of this app's own forms do (they bypass
    # full_clean() entirely), but the Django admin's stock UserAdmin does.
    USER_TYPE = (("1", "HOD"), ("2", "Staff"), ("3", "Student"), ("4", "Parent"))
    GENDER = [("M", "Male"), ("F", "Female")]


    username = None  # Removed username, using email instead
    email = models.EmailField(unique=True)
    user_type = models.CharField(default="1", choices=USER_TYPE, max_length=1)
    gender = models.CharField(max_length=1, choices=GENDER)
    profile_pic = models.ImageField()
    address = models.TextField()
    fcm_token = models.TextField(default="")  # For firebase notifications
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = CustomUserManager()

    def __str__(self):
        return self.last_name + ", " + self.first_name


class Admin(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)



class Course(models.Model):
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Student(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    # PROTECT: block deleting a course/session while students are still
    # enrolled in it, rather than silently orphaning or deleting them.
    course = models.ForeignKey(Course, on_delete=models.PROTECT, null=True, blank=False)
    session = models.ForeignKey(Session, on_delete=models.PROTECT, null=True)
    # Filled in automatically the first time a parent's link request to this
    # student is approved (see ParentStudentLink) - not collected anywhere
    # else, so it stays null until then.
    date_of_birth = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.admin.last_name + ", " + self.admin.first_name


class Staff(models.Model):
    course = models.ForeignKey(Course, on_delete=models.PROTECT, null=True, blank=False)
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)

    def __str__(self):
        return self.admin.last_name + " " + self.admin.first_name


class Parent(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    contact_number = models.CharField(max_length=20, default="")

    def __str__(self):
        return self.admin.last_name + ", " + self.admin.first_name


class ParentStudentLink(models.Model):
    RELATIONSHIP = [("mother", "Mother"), ("father", "Father"), ("guardian", "Guardian")]

    parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    relationship = models.CharField(max_length=10, choices=RELATIONSHIP)
    # What the parent entered when requesting the link - kept for the
    # admin's record even though it only backfills Student.date_of_birth
    # once (see hod_views.view_parent_link_requests), so a second, possibly
    # mistaken, submission never silently overwrites the first.
    date_of_birth = models.DateField()
    status = models.SmallIntegerField(default=0)  # 0 pending, 1 approved, -1 rejected
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True)
    decided_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='+')

    class Meta:
        unique_together = ('parent', 'student')
        ordering = ['-requested_at']

    def __str__(self):
        return f"{self.parent} -> {self.student}"


class Subject(models.Model):
    name = models.CharField(max_length=120)
    # A class can be co-taught, so this is many-to-many rather than a
    # single teacher - a nice side effect is that deleting one Staff
    # member no longer cascades into deleting a Subject (and its whole
    # attendance history) out from under any other teacher still on it.
    staff = models.ManyToManyField(Staff)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Attendance(models.Model):
    # CASCADE: attendance records are meaningless without their session/
    # subject, so deleting either should clean up the attendance taken
    # under it rather than blocking (matches AttendanceReport below).
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    date = models.DateField()
    # Whoever most recently saved this Attendance (create or re-take) -
    # lets a co-teacher taking attendance for the same class/date see who
    # already did it, instead of silently overwriting with no context.
    # Null for historical rows from before this field existed.
    taken_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AttendanceReport(models.Model):
    # CASCADE: matches every other student-owned record (leave, feedback,
    # notifications, results) — deleting a student removes their history.
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    attendance = models.ForeignKey(Attendance, on_delete=models.CASCADE)
    status = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class LeaveReportStudent(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    date = models.CharField(max_length=60)
    message = models.TextField()
    status = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set only when a parent submitted this on the student's behalf
    # (parent_apply_leave) - lets the decision views also notify the
    # parent who applied, not just the student's own account.
    applied_by_parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True, blank=True)


class LeaveReportStaff(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    date = models.CharField(max_length=60)
    message = models.TextField()
    status = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class FeedbackStudent(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    feedback = models.TextField()
    reply = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class FeedbackStaff(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    feedback = models.TextField()
    reply = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationStaff(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class NotificationStudent(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class NotificationParent(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class StudentResult(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    test = models.FloatField(default=0)
    exam = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AuditLog(models.Model):
    # SET_NULL rather than CASCADE: deleting the actor's account shouldn't
    # erase the historical record that the action happened.
    actor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='audit_actions')
    action = models.CharField(max_length=50)
    target_model = models.CharField(max_length=100)
    target_repr = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.actor} {self.action} {self.target_model} {self.target_repr}"


@receiver(post_save, sender=CustomUser)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        if instance.user_type == 1:
            Admin.objects.create(admin=instance)
        if instance.user_type == 2:
            Staff.objects.create(admin=instance)
        if instance.user_type == 3:
            Student.objects.create(admin=instance)
        if instance.user_type == 4:
            Parent.objects.create(admin=instance)


@receiver(post_save, sender=CustomUser)
def save_user_profile(sender, instance, **kwargs):
    if instance.user_type == 1:
        instance.admin.save()
    if instance.user_type == 2:
        instance.staff.save()
    if instance.user_type == 3:
        instance.student.save()
    if instance.user_type == 4:
        instance.parent.save()
