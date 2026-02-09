from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from .models import Usuario
from seguridad.models import Rol

class TailwindFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'h-4 w-4 text-brand-600 focus:ring-brand-500 border-gray-300 rounded cursor-pointer'
            else:
                existing_classes = field.widget.attrs.get('class', '')
                new_classes = 'block w-full rounded-lg border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 sm:text-sm transition-all py-2.5 hover:border-brand-400'
                field.widget.attrs['class'] = f"{existing_classes} {new_classes}".strip()
                # Añadir placeholder automáticamente si no existe
                if not field.widget.attrs.get('placeholder'):
                    field.widget.attrs['placeholder'] = field.label or field_name.replace('_', ' ').title()

class UsuarioCreationForm(TailwindFormMixin, UserCreationForm):
    class Meta:
        model = Usuario
        fields = (
            'username',
            'email',
            'first_name',
            'last_name',
            'telefono',
            'empresa',
            'sucursal_default',
            'roles',
            'sucursales',
            'departamentos',
            'is_admin_empresa',
            'estatus',
        )
    
    roles = forms.ModelMultipleChoiceField(
        queryset=Rol.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Roles Asignados (Definen permisos y acceso a departamentos)"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Opcional: Personalizar etiquetas o ayudas específicas
        self.fields['email'].required = True
        self.fields['sucursales'].widget = forms.CheckboxSelectMultiple()
        self.fields['sucursales'].queryset = self.fields['sucursales'].queryset.none()
        self.fields['departamentos'].widget = forms.CheckboxSelectMultiple()
        self.fields['departamentos'].queryset = self.fields['departamentos'].queryset.none()
        
        # Inicializar queryset de roles vacío
        from seguridad.models import Rol
        self.fields['roles'].queryset = Rol.objects.none()
        
        if 'empresa' in self.data:
            try:
                empresa_id = int(self.data.get('empresa'))
                from nucleo.models import Sucursal, Departamento
                self.fields['sucursales'].queryset = Sucursal.objects.filter(empresa_id=empresa_id).order_by('nombre')
                self.fields['departamentos'].queryset = Departamento.objects.filter(empresa_id=empresa_id).order_by('sucursal__codigo', 'nombre')
                self.fields['roles'].queryset = Rol.objects.filter(empresa_id=empresa_id, estatus=Rol.Estatus.ACTIVO).order_by('nombre')
            except (ValueError, TypeError):
                pass  # invalid input from the client; ignore and fallback to empty queryset
        elif self.instance.pk and self.instance.empresa:
             self.fields['sucursales'].queryset = self.instance.empresa.sucursales.order_by('nombre')
             self.fields['departamentos'].queryset = self.instance.empresa.departamentos.order_by('sucursal__codigo', 'nombre')
             self.fields['roles'].queryset = Rol.objects.filter(empresa=self.instance.empresa, estatus=Rol.Estatus.ACTIVO).order_by('nombre')
             # Cargar roles iniciales
             self.initial['roles'] = Rol.objects.filter(asignaciones_usuarios__usuario=self.instance)

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
            self.save_m2m() # Guarda sucursales/departamentos (campos nativos M2M)
            
            # Guardar roles manualmente
            from seguridad.models import UsuarioRol
            roles_seleccionados = self.cleaned_data.get('roles', [])
            
            # Borrar roles anteriores globales de este usuario
            # (Asumimos gestión completa desde aquí)
            user.asignaciones_roles.all().delete()
            
            for rol in roles_seleccionados:
                UsuarioRol.objects.create(usuario=user, rol=rol, empresa=user.empresa)
                
        return user

    def clean(self):
        cleaned_data = super().clean()
        empresa = cleaned_data.get('empresa')
        sucursal_default = cleaned_data.get('sucursal_default')
        is_admin_empresa = cleaned_data.get('is_admin_empresa')

        if sucursal_default and empresa:
            if sucursal_default.empresa != empresa:
                self.add_error('sucursal_default', f"La sucursal '{sucursal_default}' no pertenece a la empresa '{empresa}'.")
        
        if sucursal_default and not empresa:
             self.add_error('sucursal_default', "No puedes asignar una sucursal si no hay empresa seleccionada.")

        if is_admin_empresa and not empresa:
            self.add_error('is_admin_empresa', "Para ser administrador de empresa, el usuario debe tener una empresa asignada.")

        return cleaned_data

class UsuarioChangeForm(TailwindFormMixin, UserChangeForm):
    password = None  # Deshabilitar campo de password en el formulario de edición directa (se usa enlace aparte)

    class Meta:
        model = Usuario
        fields = (
            'username',
            'email',
            'first_name',
            'last_name',
            'telefono',
            'empresa',
            'sucursal_default',
            'roles',
            'sucursales',
            'departamentos',
            'is_admin_empresa',
            'estatus',
        )

    roles = forms.ModelMultipleChoiceField(
        queryset=Rol.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Roles Asignados (Definen permisos y acceso a departamentos)"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        self.fields['sucursales'].widget = forms.CheckboxSelectMultiple()
        self.fields['departamentos'].widget = forms.CheckboxSelectMultiple()
        
        # Inicializar queryset de roles vacío
        from seguridad.models import Rol
        self.fields['roles'].queryset = Rol.objects.none()

        # Filtrar sucursales según la empresa seleccionada
        if self.instance.pk and self.instance.empresa:
             self.fields['sucursales'].queryset = self.instance.empresa.sucursales.order_by('nombre')
             self.fields['departamentos'].queryset = self.instance.empresa.departamentos.order_by('sucursal__codigo', 'nombre')
             self.fields['roles'].queryset = Rol.objects.filter(empresa=self.instance.empresa, estatus=Rol.Estatus.ACTIVO).order_by('nombre')
             self.initial['roles'] = Rol.objects.filter(asignaciones_usuarios__usuario=self.instance)
        else:
             self.fields['sucursales'].queryset = self.fields['sucursales'].queryset.none()
             self.fields['departamentos'].queryset = self.fields['departamentos'].queryset.none()
        
        # Si hay datos POST (cuando hay error de validación), intentar filtrar por la empresa enviada
        if 'empresa' in self.data:
            try:
                empresa_id = int(self.data.get('empresa'))
                from nucleo.models import Sucursal, Departamento
                self.fields['sucursales'].queryset = Sucursal.objects.filter(empresa_id=empresa_id).order_by('nombre')
                self.fields['departamentos'].queryset = Departamento.objects.filter(empresa_id=empresa_id).order_by('sucursal__codigo', 'nombre')
                self.fields['roles'].queryset = Rol.objects.filter(empresa_id=empresa_id, estatus=Rol.Estatus.ACTIVO).order_by('nombre')
            except (ValueError, TypeError):
                pass
    
    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
            self.save_m2m()
            
            from seguridad.models import UsuarioRol
            roles_seleccionados = self.cleaned_data.get('roles', [])
            
            user.asignaciones_roles.all().delete()
            
            for rol in roles_seleccionados:
                UsuarioRol.objects.create(usuario=user, rol=rol, empresa=user.empresa)
                
        return user

    def clean(self):
        cleaned_data = super().clean()
        empresa = cleaned_data.get('empresa')
        sucursal_default = cleaned_data.get('sucursal_default')
        is_admin_empresa = cleaned_data.get('is_admin_empresa')

        if sucursal_default and empresa:
            if sucursal_default.empresa != empresa:
                self.add_error('sucursal_default', f"La sucursal '{sucursal_default}' no pertenece a la empresa '{empresa}'.")
        
        if sucursal_default and not empresa:
             self.add_error('sucursal_default', "No puedes asignar una sucursal si no hay empresa seleccionada.")
        
        if is_admin_empresa and not empresa:
            self.add_error('is_admin_empresa', "Para ser administrador de empresa, el usuario debe tener una empresa asignada.")

        return cleaned_data
