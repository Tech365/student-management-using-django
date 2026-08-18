from django import template
from django.templatetags.static import static

register = template.Library()

# Matches hod_views.DEFAULT_PROFILE_PIC / parent_views.DEFAULT_PROFILE_PIC -
# the same bundled silhouette those views fall back to for a fresh account
# with no uploaded photo. Duplicated as a literal here (not imported) to
# avoid a templatetags -> views import, which would run into Django's admin
# app-loading order.
DEFAULT_AVATAR = 'dist/img/default-150x150.png'


@register.filter
def avatar_url(profile_pic):
    """profile_pic is stored as a plain path/URL string, not a normal
    FileField upload (see hod_views.add_student) - str(profile_pic) is ''
    for any account that never got one assigned (some of the earliest
    accounts predate that convention), which renders as a broken
    <img src=""> rather than falling back to anything. Use as
    {{ user.profile_pic|avatar_url }} anywhere a profile photo is shown.

    Also guards against a real bug (fixed in parent_views.parent_register,
    but already-affected accounts still have the bad value stored): a
    bare relative path like "dist/img/default-150x150.png" with no
    leading "/" resolves against whatever page it's rendered on instead
    of the site root, breaking depending on URL depth. Every legitimate
    value here - /static/..., /media/..., or a full http(s):// URL -
    starts with "/" or a scheme, so anything else gets the same
    known-good fallback."""
    value = str(profile_pic) if profile_pic else ''
    if value and not (value.startswith('/') or '://' in value):
        value = ''
    return value or static(DEFAULT_AVATAR)
