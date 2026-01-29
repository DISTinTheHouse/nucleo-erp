from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from .models import Usuario

def apply_tailwind_styles(form):
    for field_name, field in form.fields.items():
        if isinstance(field.widget, forms.CheckboxInput):
            field.widget.attrs['class'] = 'h-4 w-4 text-brand-600 focus:ring-brand-500 border-gray-300 rounded cursor-pointer'
        else:
            existing_classes = field.widget.attrs.get('class', '')
            new_classes = 'block w-full rounded-lg border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 sm:text-sm transition-all py-2.5 hover:border-brand-400'
            field.widget.attrs['class'] = f"{existing_classes} {new_classes}".strip()

class UsuarioCreationForm(UserCreationForm):
    class Meta:
        model = Usuario
        fields = ('username', 'email', 'first_name', 'last_name', 'empresa', 'sucursal_default', 'is_staff', 'is_superuser')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_tailwind_styles(self)

class UsuarioChangeForm(UserChangeForm):
    class Meta:
        model = Usuario
        fields = ('username', 'email', 'first_name', 'last_name', 'empresa', 'sucursal_default', 'is_staff', 'is_superuser', 'estatus')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_tailwind_styles(self)
