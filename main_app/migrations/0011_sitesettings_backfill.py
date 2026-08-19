from django.db import migrations


def backfill_onboarding_completed(apps, schema_editor):
    # A DB that already has Course/Staff/Student rows is clearly an
    # already-set-up school (e.g. the existing madrasa.nzjamaat.org
    # production instance) - mark it onboarding_completed so its admin is
    # never redirected into the wizard. A genuinely empty fresh-install DB
    # gets False and enters the wizard on first admin login.
    SiteSettings = apps.get_model('main_app', 'SiteSettings')
    Course = apps.get_model('main_app', 'Course')
    Staff = apps.get_model('main_app', 'Staff')
    Student = apps.get_model('main_app', 'Student')
    already_set_up = Course.objects.exists() or Staff.objects.exists() or Student.objects.exists()
    SiteSettings.objects.get_or_create(pk=1, defaults={'onboarding_completed': already_set_up})


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0010_sitesettings'),
    ]

    operations = [
        migrations.RunPython(backfill_onboarding_completed, migrations.RunPython.noop),
    ]
