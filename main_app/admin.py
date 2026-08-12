from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import *
# Register your models here.


class CustomUserCreationForm(UserCreationForm):
    """UserCreationForm is hardcoded to auth.User's `username` field -
    CustomUser has none (email is USERNAME_FIELD instead), which made the
    Django admin's "Add user" page throw a 500 (FieldError: Unknown
    field(s) (username)) on every attempt."""

    class Meta:
        model = CustomUser
        fields = ('email',)


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = CustomUser
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same reasoning as every other edit form in this app - don't
        # force a photo re-upload just to change something else.
        self.fields['profile_pic'].required = False


class UserModel(UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser
    ordering = ('email',)
    list_display = ('email', 'first_name', 'last_name', 'user_type', 'is_active', 'is_staff')
    list_filter = ('user_type', 'is_active', 'is_staff', 'is_superuser')
    search_fields = ('email', 'first_name', 'last_name')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'gender', 'address', 'profile_pic', 'user_type')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            # profile_pic deliberately left out here (unlike `fieldsets`
            # above) - ImageField has no blank=True, so requiring a photo
            # just to bootstrap an account via this rarely-used admin
            # screen isn't worth the friction.
            'fields': ('email', 'first_name', 'last_name', 'user_type', 'gender', 'address', 'password1', 'password2'),
        }),
    )


admin.site.register(CustomUser, UserModel)
admin.site.register(Staff)
admin.site.register(Student)
admin.site.register(Course)
admin.site.register(Subject)
admin.site.register(Session)
