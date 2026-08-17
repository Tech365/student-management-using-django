from django.utils.deprecation import MiddlewareMixin
from django.urls import reverse
from django.shortcuts import redirect

from .utils import user_roles


class LoginCheckMiddleWare(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        modulename = view_func.__module__
        user = request.user # Who is the current user ?
        if user.is_authenticated:
            roles = dict(user_roles(user))
            active_role = request.session.get('active_role')
            if active_role not in roles:
                # Missing/stale/revoked active_role - e.g. a session that
                # pre-dates multi-role support (self.client.login() in
                # tests never sets it either), or an admin just removed
                # this exact role out from under a logged-in user. Self-heal
                # to a valid role rather than hard-failing the request -
                # prefers the account's primary role (user_type) when it's
                # still one they actually have.
                active_role = user.user_type if user.user_type in roles else next(iter(roles), None)
                if active_role is None: # No role at all - nothing to route to
                    return redirect(reverse('login_page'))
                request.session['active_role'] = active_role
            if active_role == '1': # Is it the HOD/Admin
                if modulename == 'main_app.student_views':
                    return redirect(reverse('admin_home'))
            elif active_role == '2': #  Staff :-/ ?
                if modulename == 'main_app.student_views' or modulename == 'main_app.hod_views':
                    return redirect(reverse('staff_home'))
            elif active_role == '3': # ... or Student ?
                if modulename == 'main_app.hod_views' or modulename == 'main_app.staff_views':
                    return redirect(reverse('student_home'))
            elif active_role == '4': # ... or Parent ?
                if modulename in ('main_app.hod_views', 'main_app.staff_views', 'main_app.student_views'):
                    return redirect(reverse('parent_home'))
        else:
            if request.path == reverse('login_page') or modulename == 'django.contrib.auth.views' or request.path == reverse('user_login') or request.path == reverse('showFirebaseJS') or request.path == reverse('privacy_notice') or request.path == reverse('parent_register') or request.path == reverse('parent_register_get_students'): # If the path is login, auth, the service worker (needed pre-login for PWA install), the privacy notice, or public parent registration, pass
                pass
            else:
                return redirect(reverse('login_page'))
