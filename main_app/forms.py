import os
from datetime import date, timedelta

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.files.uploadedfile import UploadedFile
from django.forms.widgets import DateInput, TextInput

from .models import *

ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
MAX_PROFILE_PIC_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_PENDING_LINKS_PER_STUDENT = 3


class FormSettings(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(FormSettings, self).__init__(*args, **kwargs)
        # Here make some changes such as:
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'


class CustomUserForm(FormSettings):
    # Subclasses used only for admin-side "Add X" screens set this True, so
    # an email that already belongs to another role isn't rejected here -
    # the view decides whether to attach the new role to that existing
    # account or reject a duplicate. Public/self-service forms (parent
    # registration, profile edits) leave this False and keep hard-blocking
    # duplicate emails, since those must never be able to touch an account
    # they don't already own.
    allow_existing_email_on_create = False

    email = forms.EmailField(required=True)
    gender = forms.ChoiceField(choices=[('M', 'Male'), ('F', 'Female')])
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)
    address = forms.CharField(widget=forms.Textarea)
    password = forms.CharField(widget=forms.PasswordInput)
    widget = {
        'password': forms.PasswordInput(),
    }
    profile_pic = forms.ImageField()

    def __init__(self, *args, **kwargs):
        super(CustomUserForm, self).__init__(*args, **kwargs)

        if kwargs.get('instance'):
            instance = kwargs.get('instance').admin.__dict__
            self.fields['password'].required = False
            self.fields['profile_pic'].required = False
            for field in CustomUserForm.Meta.fields:
                self.fields[field].initial = instance.get(field)
            if self.instance.pk is not None:
                self.fields['password'].widget.attrs['placeholder'] = "Fill this only if you wish to update password"

    def clean_password(self):
        # Blank is fine (means "keep the existing password" on edit,
        # already enforced by required=False there) - only validate an
        # actual new password against AUTH_PASSWORD_VALIDATORS.
        password = self.cleaned_data.get('password')
        if password:
            validate_password(password)
        return password

    def clean_profile_pic(self):
        # ImageField already verifies the content is a genuine image
        # (Pillow-backed), but the saved filename's extension is taken
        # from the upload verbatim (FileSystemStorage.save) - restrict it
        # to a safe allowlist too, and cap the size so this can't be used
        # for a disk-filling upload (nothing else in the app limits it,
        # and parent_register is reachable with no account at all).
        # On an edit with no new file chosen, Django's FileField.clean()
        # falls back to the field's `initial` - the existing photo's
        # stored URL *string*, not an uploaded file - so only validate
        # when a real upload came through.
        passport = self.cleaned_data.get('profile_pic')
        if passport and isinstance(passport, UploadedFile):
            ext = os.path.splitext(passport.name)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                raise forms.ValidationError(
                    "Unsupported image type. Use JPG, PNG, GIF, or WEBP.")
            if passport.size > MAX_PROFILE_PIC_BYTES:
                raise forms.ValidationError("Image must be smaller than 5MB.")
        return passport

    def clean_email(self, *args, **kwargs):
        formEmail = self.cleaned_data['email'].lower()
        if self.instance.pk is None:  # Insert
            if not self.allow_existing_email_on_create and CustomUser.objects.filter(email=formEmail).exists():
                raise forms.ValidationError(
                    "The given email is already registered")
        else:  # Update
            dbEmail = self.Meta.model.objects.get(
                id=self.instance.pk).admin.email.lower()
            if dbEmail != formEmail:  # There has been changes
                if CustomUser.objects.filter(email=formEmail).exists():
                    raise forms.ValidationError("The given email is already registered")

        return formEmail

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'gender',  'password','profile_pic', 'address' ]


class StudentForm(CustomUserForm):
    # Admin-only "Add Student" can attach the Student role to an email that
    # already belongs to another role (e.g. an existing Staff member) -
    # see hod_views.add_student.
    allow_existing_email_on_create = True

    def __init__(self, *args, **kwargs):
        super(StudentForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Student
        fields = CustomUserForm.Meta.fields + \
            ['course', 'session']
        labels = {'course': 'Class'}


class AdminForm(CustomUserForm):
    allow_existing_email_on_create = True

    def __init__(self, *args, **kwargs):
        super(AdminForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Admin
        fields = CustomUserForm.Meta.fields


class StaffForm(CustomUserForm):
    """No 'course'/class field: which classes a staff member belongs to
    is entirely determined by which Subjects they're assigned to teach
    (a teacher can teach subjects in more than one class), not by a
    single field on Staff."""

    allow_existing_email_on_create = True

    def __init__(self, *args, **kwargs):
        super(StaffForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Staff
        fields = CustomUserForm.Meta.fields


class CourseForm(FormSettings):
    def __init__(self, *args, **kwargs):
        super(CourseForm, self).__init__(*args, **kwargs)

    class Meta:
        fields = ['name']
        model = Course


class SubjectForm(FormSettings):

    def __init__(self, *args, **kwargs):
        super(SubjectForm, self).__init__(*args, **kwargs)

    class Meta:
        model = Subject
        fields = ['name', 'staff', 'course']
        labels = {'course': 'Class'}


class SessionForm(FormSettings):
    # Not the model's plain CharField - a checkbox per weekday is a much
    # more usable way to pick "which days does this session actually
    # hold classes on" than typing "0,3,5" into a text box.
    school_days = forms.MultipleChoiceField(
        choices=Session.WEEKDAYS, widget=forms.CheckboxSelectMultiple,
        required=False, label="School Days",
        help_text="Leave all unchecked for no restriction (the default for sessions created before this existed).")

    def __init__(self, *args, **kwargs):
        super(SessionForm, self).__init__(*args, **kwargs)
        if self.instance.pk and self.instance.school_days:
            # ModelForm's own model_to_dict-based self.initial already has
            # a "school_days" entry (the raw comma-string) which otherwise
            # wins over field.initial when rendering - override that
            # directly, not the field, or the checkboxes stay unchecked.
            self.initial['school_days'] = self.instance.school_days.split(',')
        # FormSettings.__init__ (already ran, above) blindly stamps
        # class="form-control" onto every visible field, including each
        # individual checkbox <input> here - Bootstrap's .form-control is
        # display:block/width:100%, which blows every checkbox up into a
        # full-width bar instead of a small toggle. Not what any checkbox
        # field wants, so drop it back off.
        self.fields['school_days'].widget.attrs.pop('class', None)

    def clean_school_days(self):
        return ','.join(self.cleaned_data.get('school_days', []))

    class Meta:
        model = Session
        fields = '__all__'
        widgets = {
            'start_year': DateInput(attrs={'type': 'date'}),
            'end_year': DateInput(attrs={'type': 'date'}),
        }


class LeaveReportStaffForm(FormSettings):
    def __init__(self, *args, **kwargs):
        super(LeaveReportStaffForm, self).__init__(*args, **kwargs)

    class Meta:
        model = LeaveReportStaff
        fields = ['date', 'message']
        widgets = {
            'date': DateInput(attrs={'type': 'date'}),
        }


class FeedbackStaffForm(FormSettings):

    def __init__(self, *args, **kwargs):
        super(FeedbackStaffForm, self).__init__(*args, **kwargs)

    class Meta:
        model = FeedbackStaff
        fields = ['feedback']


class LeaveReportStudentForm(FormSettings):
    def __init__(self, *args, **kwargs):
        super(LeaveReportStudentForm, self).__init__(*args, **kwargs)

    class Meta:
        model = LeaveReportStudent
        fields = ['date', 'message']
        widgets = {
            'date': DateInput(attrs={'type': 'date'}),
        }


class FeedbackStudentForm(FormSettings):

    def __init__(self, *args, **kwargs):
        super(FeedbackStudentForm, self).__init__(*args, **kwargs)

    class Meta:
        model = FeedbackStudent
        fields = ['feedback']


class StudentEditForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(StudentEditForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Student
        fields = CustomUserForm.Meta.fields 


class StaffEditForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(StaffEditForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Staff
        fields = CustomUserForm.Meta.fields


class ContactNumberValidationMixin:
    """Shared by both places a Parent's contact_number is collected -
    the model field itself is a bare CharField (needs to hold numbers
    with a leading 0, so it can't be an IntegerField), so nothing else
    stops someone typing letters into it without this."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Hints mobile browsers to show a numeric keypad - a nicety on
        # top of clean_contact_number below, which is what actually
        # enforces this.
        self.fields['contact_number'].widget.attrs.update(
            {'inputmode': 'numeric', 'pattern': '[0-9]*'})

    def clean_contact_number(self):
        contact_number = self.cleaned_data.get('contact_number', '').strip()
        if not contact_number.isdigit():
            raise forms.ValidationError("Contact number must contain digits only.")
        return contact_number


class ParentRegistrationForm(ContactNumberValidationMixin, CustomUserForm):
    """Public self-registration - unlike every other role here, this
    account isn't created by an HOD who'd have a photo on hand, so a
    fresh photo is never required."""

    def __init__(self, *args, **kwargs):
        super(ParentRegistrationForm, self).__init__(*args, **kwargs)
        self.fields['profile_pic'].required = False

    class Meta(CustomUserForm.Meta):
        model = Parent
        fields = CustomUserForm.Meta.fields + ['contact_number']


class ParentEditForm(ContactNumberValidationMixin, CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(ParentEditForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Parent
        fields = CustomUserForm.Meta.fields + ['contact_number']


class GrantParentRoleForm(ContactNumberValidationMixin, forms.Form):
    """Admin-only "attach the Parent role to an existing account" form -
    deliberately NOT a way to create a brand-new Parent identity: that
    still only ever happens through public self-registration + the
    existing link-request/approval flow. See hod_views.grant_parent_role,
    which rejects an email that doesn't already belong to an account."""

    email = forms.EmailField()
    contact_number = forms.CharField(max_length=20)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class ParentLinkRequestForm(forms.Form):
    """Not a ModelForm - it writes to both Student (indirectly, via the
    picked id) and ParentStudentLink, so there's no single model to bind
    to the way FormSettings' other subclasses do."""

    course = forms.ModelChoiceField(queryset=Course.objects.all(), label="Class")
    student = forms.ModelChoiceField(queryset=Student.objects.none(), label="Child's Name")
    relationship = forms.ChoiceField(choices=ParentStudentLink.RELATIONSHIP)
    date_of_birth = forms.DateField(widget=DateInput(attrs={'type': 'date'}), label="Child's Date of Birth")

    def __init__(self, *args, **kwargs):
        super(ParentLinkRequestForm, self).__init__(*args, **kwargs)
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'
        # The dropdown itself is populated client-side, one class at a time
        # (get_students_for_registration) - an empty queryset keeps the
        # unbound/initial render from dumping the entire student roster into
        # the page source. On a real submission the form is bound, and the
        # queryset needs to include the submitted id for clean() to accept
        # it, so widen it only then.
        if self.is_bound:
            self.fields['student'].queryset = Student.objects.all()

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get('date_of_birth')
        if dob:
            if dob > date.today():
                raise forms.ValidationError("Date of birth can't be in the future.")
            if dob < date.today() - timedelta(days=120 * 365):
                raise forms.ValidationError("Please enter a valid date of birth.")
        return dob

    def clean_student(self):
        # Caps how many *pending* requests a single student can accumulate
        # at once, regardless of which parent is asking - without this,
        # nothing stopped scripted registrations from flooding the
        # approval queue with fake claims on the same kid (rate limiting
        # on parent_register slows that down, but doesn't cap it).
        student = self.cleaned_data.get('student')
        if student and ParentStudentLink.objects.filter(student=student, status=0).count() >= MAX_PENDING_LINKS_PER_STUDENT:
            raise forms.ValidationError(
                "This student already has the maximum number of pending link "
                "requests. Please contact the school directly.")
        return student


# A parent can have more than one kid at the school, so both registration
# and "link another kid" let them submit several ParentLinkRequestForms at
# once (min_num=1/validate_min: at least one child is required). extra=0,
# not 1: Django's own form count is max(initial_forms, min_num) + extra,
# so extra=1 here would render min_num(1) + extra(1) = 2 blocks by
# default - forcing every parent to fill in a second child before they
# could submit at all. The one required block still renders correctly
# because min_num=1 already guarantees it; "+ Add Another Child" clones
# more in on demand.
ParentLinkRequestFormSet = forms.formset_factory(
    ParentLinkRequestForm, extra=0, min_num=1, validate_min=True)


class EditResultForm(FormSettings):
    session_year = forms.ModelChoiceField(
        label="Session Year", queryset=Session.objects.none(), required=True)

    def __init__(self, *args, **kwargs):
        super(EditResultForm, self).__init__(*args, **kwargs)
        # Set per-request rather than as a class attribute, so newly added
        # sessions show up without needing a server restart.
        self.fields['session_year'].queryset = Session.objects.all()

    class Meta:
        model = StudentResult
        fields = ['session_year', 'subject', 'student', 'test', 'exam']


class CSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV File",
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.csv'}),
    )

    def clean_csv_file(self):
        csv_file = self.cleaned_data['csv_file']
        if not csv_file.name.lower().endswith('.csv'):
            raise forms.ValidationError("Please upload a .csv file.")
        return csv_file
