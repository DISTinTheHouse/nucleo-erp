from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from .views import SuperuserRequiredMixin
from .models import (
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, SatClaveProdServ, SatClaveUnidad
)

# --- CATÁLOGOS SAT ---
class SatGenericListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    template_name = 'nucleo/sat_catalogo_list.html'
    context_object_name = 'items'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['titulo'] = self.titulo
        context['subtitle'] = "Catálogos SAT (Solo Lectura)"
        return context

class SatRegimenFiscalListView(SatGenericListView):
    model = SatRegimenFiscal
    titulo = "Régimen Fiscal"

class SatUsoCfdiListView(SatGenericListView):
    model = SatUsoCfdi
    titulo = "Uso CFDI"

class SatMetodoPagoListView(SatGenericListView):
    model = SatMetodoPago
    titulo = "Método de Pago"

class SatFormaPagoListView(SatGenericListView):
    model = SatFormaPago
    titulo = "Forma de Pago"

class SatClaveProdServListView(SatGenericListView):
    model = SatClaveProdServ
    titulo = "Clave Producto/Servicio"
    paginate_by = 50

class SatClaveUnidadListView(SatGenericListView):
    model = SatClaveUnidad
    titulo = "Clave Unidad"
    paginate_by = 50
