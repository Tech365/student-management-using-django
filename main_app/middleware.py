from django.utils.deprecation import MiddlewareMixin
from django.urls import reverse
from django.shortcuts import redirect


class LoginCheckMiddleWare(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        modulename = view_func.__module__
        user = request.user # Who is the current user ?
        if user.is_authenticated:
            if user.user_type == '1': # Is it the HOD/Admin
                if modulename == 'main_app.student_views':
                    return redirect(reverse('admin_home'))
            elif user.user_type == '2': #  Staff :-/ ?
                if modulename == 'main_app.student_views' or modulename == 'main_app.hod_views':
                    return redirect(reverse('staff_home'))
            elif user.user_type == '3': # ... or Student ?
                if modulename == 'main_app.hod_views' or modulename == 'main_app.staff_views':
                    return redirect(reverse('student_home'))
            elif user.user_type == '4': # ... or Parent ?
                if modulename in ('main_app.hod_views', 'main_app.staff_views', 'main_app.student_views'):
                    return redirect(reverse('parent_home'))
            else: # None of the aforementioned ? Please take the user to login page
                return redirect(reverse('login_page'))
        else:
            if request.path == reverse('login_page') or modulename == 'django.contrib.auth.views' or request.path == reverse('user_login') or request.path == reverse('showFirebaseJS') or request.path == reverse('privacy_notice') or request.path == reverse('parent_register') or request.path == reverse('parent_register_get_students'): # If the path is login, auth, the service worker (needed pre-login for PWA install), the privacy notice, or public parent registration, pass
                pass
            else:
                return redirect(reverse('login_page'))
