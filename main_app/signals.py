import logging

from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver

from .utils import client_ip

logger = logging.getLogger(__name__)


@receiver(user_login_failed)
def log_failed_login(sender, credentials, request, **kwargs):
    """Django's auth machinery sends this signal whenever authenticate()
    fails to find a matching, active user - from the app's own login
    (doLogin) and from Django's built-in /admin/login/ alike, since both
    ultimately call authenticate(). rate_limited() already blunts brute-
    forcing either entry point, but neither left any record of what was
    actually attempted, so there was no way to notice a slow/distributed
    credential-stuffing attempt that stays under that threshold."""
    username = credentials.get('username') or credentials.get('email')
    ip = client_ip(request) if request is not None else 'unknown'
    logger.warning("Failed login attempt for %r from %s", username, ip)
