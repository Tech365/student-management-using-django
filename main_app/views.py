import json
import logging

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render, reverse

from .models import Attendance, Session, Staff, Subject
from .utils import rate_limited, role_home_url, user_roles

logger = logging.getLogger(__name__)

# Create your views here.


def login_page(request):
    if request.user.is_authenticated:
        roles = dict(user_roles(request.user))
        active_role = request.session.get('active_role')
        if active_role in roles:
            return redirect(role_home_url(active_role))
        return redirect(reverse("choose_role"))
    return render(request, 'main_app/login.html')


def privacy_notice(request):
    return render(request, 'main_app/privacy_notice.html')


def doLogin(request, **kwargs):
    if request.method != 'POST':
        return HttpResponse("<h4>Denied</h4>")
    else:
        if rate_limited(request, 'login', max_attempts=10, window_seconds=300):
            messages.error(request, "Too many login attempts. Please wait a few minutes and try again.")
            return redirect("/")
        #Authenticate
        user = authenticate(request, username=request.POST.get('email'), password=request.POST.get('password'))
        if user is not None:
            roles = user_roles(user)
            if not roles:
                messages.error(request, "Your account has no role assigned. Please contact the Madrasa Admin.")
                return redirect("/")
            login(request, user)
            if len(roles) == 1:
                request.session['active_role'] = roles[0][0]
                return redirect(role_home_url(roles[0][0]))
            return redirect(reverse("choose_role"))
        else:
            messages.error(request, "Invalid details")
            return redirect("/")


def choose_role(request):
    """Lets a multi-role account (e.g. Staff and Parent on the same email)
    pick which dashboard to use for this session - also doubles as the
    "Switch Role" destination from the sidebar. Safe to link to
    unconditionally: a user with zero or one role is redirected straight
    through instead of seeing an empty/pointless picker."""
    if not request.user.is_authenticated:
        return redirect(reverse('login_page'))
    roles = user_roles(request.user)
    if len(roles) <= 1:
        return redirect(role_home_url(roles[0][0]) if roles else reverse('login_page'))
    if request.method == 'POST':
        role_code = request.POST.get('role')
        if role_code not in dict(roles):
            messages.error(request, "Invalid role selection.")
            return redirect(reverse('choose_role'))
        request.session['active_role'] = role_code
        return redirect(role_home_url(role_code))
    return render(request, 'main_app/choose_role.html', {'roles': roles, 'page_title': 'Choose how to continue'})


def logout_user(request):
    if request.user is not None:
        logout(request)
    return redirect("/")


def get_attendance(request):
    # Shared by the HOD's admin_view_attendance page (unrestricted, same
    # as every other admin_view_* endpoint) and the staff update-attendance
    # page (must be scoped to subjects this teacher actually teaches, same
    # as staff_views.get_students/save_attendance) - anyone else has no
    # legitimate reason to see another class's attendance-taken dates.
    active_role = request.session.get('active_role')
    if active_role not in ('1', '2'):
        return JsonResponse({'error': 'Not authorized'}, status=403)
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')
    try:
        if active_role == '2':
            staff = get_object_or_404(Staff, admin=request.user)
            subject = get_object_or_404(Subject, id=subject_id, staff=staff)
        else:
            subject = get_object_or_404(Subject, id=subject_id)
        session = get_object_or_404(Session, id=session_id)
        attendance = Attendance.objects.filter(subject=subject, session=session)
        attendance_list = []
        for attd in attendance:
            data = {
                    "id": attd.id,
                    "attendance_date": str(attd.date),
                    "session": attd.session.id
                    }
            attendance_list.append(data)
        return JsonResponse(json.dumps(attendance_list), safe=False)
    except Exception as e:
        logger.exception("Failed to fetch attendance")
        return JsonResponse({'error': str(e)}, status=400)


def showFirebaseJS(request):
    data = """
    // Give the service worker access to Firebase Messaging.
// Note that you can only use Firebase Messaging here, other Firebase libraries
// are not available in the service worker.
// Wrapped in try/catch so a blocked/unreachable Firebase CDN can't stop the
// offline-caching handlers below from registering.
try {
    importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-app.js');
    importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-messaging.js');

    // Initialize the Firebase app in the service worker by passing in
    // your app's Firebase config object.
    // https://firebase.google.com/docs/web/setup#config-object
    firebase.initializeApp({
        apiKey: "AIzaSyBarDWWHTfTMSrtc5Lj3Cdw5dEvjAkFwtM",
        authDomain: "sms-with-django.firebaseapp.com",
        databaseURL: "https://sms-with-django.firebaseio.com",
        projectId: "sms-with-django",
        storageBucket: "sms-with-django.appspot.com",
        messagingSenderId: "945324593139",
        appId: "1:945324593139:web:03fa99a8854bbd38420c86",
        measurementId: "G-2F2RXTL9GT"
    });

    // Retrieve an instance of Firebase Messaging so that it can handle background
    // messages.
    const messaging = firebase.messaging();
    messaging.setBackgroundMessageHandler(function (payload) {
        const notification = JSON.parse(payload);
        const notificationOption = {
            body: notification.body,
            icon: notification.icon
        }
        return self.registration.showNotification(payload.notification.title, notificationOption);
    });
} catch (e) {
    console.warn('Firebase messaging unavailable in service worker:', e);
}

// PWA offline support: cache GET responses as they're fetched, and fall back
// to the cache (or the cached homepage) when the network is unavailable.
const PWA_CACHE = 'sms-pwa-v1';

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((key) => key !== PWA_CACHE).map((key) => caches.delete(key)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') {
        return;
    }
    event.respondWith(
        fetch(event.request)
            .then((response) => {
                const copy = response.clone();
                caches.open(PWA_CACHE).then((cache) => cache.put(event.request, copy));
                return response;
            })
            .catch(() =>
                caches.match(event.request).then((cached) => cached || caches.match('/'))
            )
    );
});
    """
    return HttpResponse(data, content_type='application/javascript')
