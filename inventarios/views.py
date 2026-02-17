from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from .models import Almacen, Ubicacion


class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser


class AlmacenListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Almacen
    template_name = 'inventarios/almacen_list.html'
    context_object_name = 'almacenes'


class UbicacionListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Ubicacion
    template_name = 'inventarios/ubicacion_list.html'
    context_object_name = 'ubicaciones'
