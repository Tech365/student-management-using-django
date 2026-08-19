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


DEFAULT_LOGO_CIRCULAR = 'dist/img/madrasa-emblem-circular.png'
DEFAULT_LOGO_FULL = 'dist/img/madrasa-logo-full.png'


@register.filter
def site_logo_url(site_settings, variant='circular'):
    """{{ site_settings|site_logo_url }} / {{ site_settings|site_logo_url:'full' }}
    - falls back to this install's original bundled logo when no school
    logo has been uploaded yet, same fallback pattern as avatar_url above.
    Both variants (the small circular emblem used in the navbar/sidebar,
    and the full logo used on full-page screens like login) fall back to
    the SAME uploaded logo once one is set - a single upload covers both
    by design, not a bug."""
    if site_settings and site_settings.logo_url:
        return site_settings.logo_url
    return static(DEFAULT_LOGO_FULL if variant == 'full' else DEFAULT_LOGO_CIRCULAR)


@register.filter
def without_page(query_dict):
    """request.GET with the 'page' key stripped and re-encoded, for
    pagination links - so moving to page 2 of a filtered report doesn't
    silently drop the filters (course, date range, ...) that produced the
    list being paged through in the first place."""
    qd = query_dict.copy()
    qd.pop('page', None)
    return qd.urlencode()
