from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from .models import Almacen, Ubicacion
from .forms import AlmacenForm, UbicacionForm


class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser


class AlmacenListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Almacen
    template_name = 'inventarios/almacen_list.html'
    context_object_name = 'almacenes'


class AlmacenCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Almacen
    form_class = AlmacenForm
    template_name = 'inventarios/almacen_form.html'
    success_url = reverse_lazy('inventarios:almacen_list')


class AlmacenUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Almacen
    form_class = AlmacenForm
    template_name = 'inventarios/almacen_form.html'
    success_url = reverse_lazy('inventarios:almacen_list')


class AlmacenDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Almacen
    template_name = 'inventarios/almacen_confirm_delete.html'
    success_url = reverse_lazy('inventarios:almacen_list')


class UbicacionListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Ubicacion
    template_name = 'inventarios/ubicacion_list.html'
    context_object_name = 'ubicaciones'


class UbicacionCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Ubicacion
    form_class = UbicacionForm
    template_name = 'inventarios/ubicacion_form.html'
    success_url = reverse_lazy('inventarios:ubicacion_list')


class UbicacionUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Ubicacion
    form_class = UbicacionForm
    template_name = 'inventarios/ubicacion_form.html'
    success_url = reverse_lazy('inventarios:ubicacion_list')


class UbicacionDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Ubicacion
    template_name = 'inventarios/ubicacion_confirm_delete.html'
    success_url = reverse_lazy('inventarios:ubicacion_list')

