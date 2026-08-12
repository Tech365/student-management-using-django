from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        try:
            user = UserModel.objects.get(email=username)
        except UserModel.DoesNotExist:
            # Run the password hasher anyway, same as Django's own
            # ModelBackend - otherwise a nonexistent email returns near-
            # instantly while a real one takes as long as a full hash
            # comparison, letting a timing attack enumerate registered
            # emails from the login form.
            UserModel().set_password(password)
        else:
            # user_can_authenticate() checks is_active — without this,
            # a deactivated account could still log in.
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None
