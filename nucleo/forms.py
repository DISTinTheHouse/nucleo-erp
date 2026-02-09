from django import forms
from django.utils.text import slugify
from .models import Empresa, Sucursal, Departamento, Moneda, Impuesto, UnidadMedida
from .utils import validate_rfc, check_sat_status_mock

class TailwindModelForm(forms.ModelForm):
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

class EmpresaForm(TailwindModelForm):
    # Sobreescribimos codigo para que sea menos estricto en la entrada
    codigo = forms.CharField(
        label="Código (Slug)",
        help_text="Identificador único (se convertirá a minúsculas y guiones automáticamente)",
        max_length=32
    )

    class Meta:
        model = Empresa
        fields = '__all__'
        widgets = {
            'config_json': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_codigo(self):
        codigo = self.cleaned_data['codigo']
        # Auto-slugify
        slug = slugify(codigo)
        if not slug:
            raise forms.ValidationError("El código debe contener caracteres válidos (letras y números).")
        
        # Verificar unicidad (excluyendo la propia instancia si es edición)
        qs = Empresa.objects.filter(codigo=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        
        if qs.exists():
            raise forms.ValidationError(f"El código '{slug}' ya está en uso por otra empresa.")
            
        return slug

    def clean_rfc(self):
        rfc = self.cleaned_data.get('rfc')
        if rfc:
            rfc = rfc.upper().strip()
            
            # 1. Validación Estructural y Checksum
            es_valido, mensaje = validate_rfc(rfc)
            if not es_valido:
                raise forms.ValidationError(mensaje)
            
            # 2. Validación de Existencia (Simulación / API Placeholder)
            # En un entorno real, esto conectaría a un servicio SAT
            sat_status = check_sat_status_mock(rfc)
            if not sat_status.get('exists'):
                raise forms.ValidationError("El RFC tiene un formato válido pero NO se encuentra registrado en el SAT.")
                
        return rfc

class SucursalForm(TailwindModelForm):
    class Meta:
        model = Sucursal
        fields = '__all__'
        widgets = {
            'empresa': forms.Select(attrs={'class': 'form-select'}),
            'estatus': forms.Select(attrs={'class': 'form-select'}),
        }
    
    def clean(self):
        cleaned_data = super().clean()
        empresa = cleaned_data.get('empresa')
        codigo = cleaned_data.get('codigo')
        
        if empresa and codigo:
            # Validar unicidad empresa-codigo
            qs = Sucursal.objects.filter(empresa=empresa, codigo=codigo)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            
            if qs.exists():
                self.add_error('codigo', f"El código '{codigo}' ya existe en la empresa {empresa}.")
        
        return cleaned_data

class DepartamentoForm(TailwindModelForm):
    class Meta:
        model = Departamento
        fields = '__all__'

class MonedaForm(TailwindModelForm):
    class Meta:
        model = Moneda
        fields = '__all__'

class ImpuestoForm(TailwindModelForm):
    class Meta:
        model = Impuesto
        fields = '__all__'

class UnidadMedidaForm(TailwindModelForm):
    class Meta:
        model = UnidadMedida
        fields = '__all__'
