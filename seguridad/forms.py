from django import forms
from .models import Rol

class RolForm(forms.ModelForm):
    class Meta:
        model = Rol
        fields = ['empresa', 'codigo', 'nombre', 'clave_departamento', 'descripcion', 'estatus']
        widgets = {
            'descripcion': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Styling similar to other forms
        for field_name, field in self.fields.items():
            if field_name == 'estatus':
                 field.widget.attrs['class'] = 'block w-full rounded-lg border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 sm:text-sm transition-all py-2.5'
            elif field_name == 'empresa':
                 field.widget.attrs['class'] = 'block w-full rounded-lg border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 sm:text-sm transition-all py-2.5'
            else:
                field.widget.attrs['class'] = 'block w-full rounded-lg border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 sm:text-sm transition-all py-2.5 hover:border-brand-400'
