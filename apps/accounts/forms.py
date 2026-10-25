from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    UserCreationForm,
)
from django.contrib.auth.models import User

from .models import COMMON_TIMEZONES, UserPreference, validate_timezone_name


class RegistrationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            css_class = "form-control"
            if self.is_bound and field_name in self.errors:
                css_class += " is-invalid"
            field.widget.attrs["class"] = css_class
        self.fields["username"].widget.attrs.setdefault("autocomplete", "username")
        self.fields["password1"].widget.attrs.setdefault("autocomplete", "new-password")
        self.fields["password2"].widget.attrs.setdefault("autocomplete", "new-password")


class AccountLoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Invalid username or password. Please try again.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            css_class = "form-control"
            if self.is_bound and (field_name in self.errors or self.non_field_errors()):
                css_class += " is-invalid"
            field.widget.attrs["class"] = css_class
        self.fields["username"].widget.attrs.setdefault("autocomplete", "username")
        self.fields["password"].widget.attrs.setdefault("autocomplete", "current-password")


class ProfileDisplayNameForm(forms.Form):
    display_name = forms.CharField(
        label="Display name",
        max_length=150,
        required=True,
        widget=forms.TextInput(
            attrs={
                "id": "settings-display-name",
                "class": "form-control form-control-sm",
                "autocomplete": "name",
            }
        ),
    )

    def clean_display_name(self):
        val = (self.cleaned_data.get("display_name") or "").strip()
        if not val:
            raise forms.ValidationError("Display name cannot be blank.")
        return val


class UserPreferencesForm(forms.ModelForm):
    timezone = forms.ChoiceField(
        label="Time zone",
        choices=[(tz, tz) for tz in COMMON_TIMEZONES],
        validators=[validate_timezone_name],
        widget=forms.Select(
            attrs={
                "id": "settings-timezone",
                "class": "form-select form-select-sm",
            }
        ),
    )

    class Meta:
        model = UserPreference
        fields = ("timezone", "week_starts_on")
        widgets = {
            "week_starts_on": forms.Select(
                attrs={
                    "id": "settings-week-start",
                    "class": "form-select form-select-sm",
                }
            ),
        }
        labels = {
            "week_starts_on": "Week starts on",
        }


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            css_class = "form-control form-control-sm"
            if self.is_bound and field_name in self.errors:
                css_class += " is-invalid"
            field.widget.attrs["class"] = css_class
