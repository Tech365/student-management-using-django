import logging

from django.shortcuts import get_object_or_404, render, redirect
from django.views import View
from django.contrib import messages
from .models import Subject, Staff, Student, StudentResult
from .forms import EditResultForm
from django.urls import reverse

logger = logging.getLogger(__name__)


class EditResultView(View):
    def get(self, request, *args, **kwargs):
        resultForm = EditResultForm()
        staff = get_object_or_404(Staff, admin=request.user)
        resultForm.fields['subject'].queryset = Subject.objects.filter(staff=staff)
        context = {
            'form': resultForm,
            'page_title': "Edit Student's Result"
        }
        return render(request, "staff_template/edit_student_result.html", context)

    def post(self, request, *args, **kwargs):
        form = EditResultForm(request.POST)
        staff = get_object_or_404(Staff, admin=request.user)
        context = {'form': form, 'page_title': "Edit Student's Result"}
        if form.is_valid():
            try:
                student = form.cleaned_data.get('student')
                subject = form.cleaned_data.get('subject')
                test = form.cleaned_data.get('test')
                exam = form.cleaned_data.get('exam')
                # Reject subjects that don't belong to the requesting staff
                # member, so a tampered subject id can't be used to edit
                # another teacher's results.
                if not subject.staff.filter(id=staff.id).exists():
                    messages.warning(request, "Result Could Not Be Updated")
                    return render(request, "staff_template/edit_student_result.html", context)
                # Same reasoning for the student - otherwise a tampered
                # student id from outside this subject's class could be
                # used to edit results they never had.
                if student.course_id != subject.course_id:
                    messages.warning(request, "Result Could Not Be Updated")
                    return render(request, "staff_template/edit_student_result.html", context)
                result = StudentResult.objects.get(student=student, subject=subject)
                result.exam = exam
                result.test = test
                result.save()
                messages.success(request, "Result Updated")
                return redirect(reverse('edit_student_result'))
            except Exception as e:
                logger.exception('Unhandled error in post')
                messages.warning(request, "Result Could Not Be Updated")
        else:
            messages.warning(request, "Result Could Not Be Updated")
        return render(request, "staff_template/edit_student_result.html", context)
