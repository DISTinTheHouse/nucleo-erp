from django import forms
from .models import Almacen, Ubicacion


class AlmacenForm(forms.ModelForm):
    class Meta:
        model = Almacen
        fields = '__all__'


class UbicacionForm(forms.ModelForm):
    class Meta:
        model = Ubicacion
        fields = '__all__'

