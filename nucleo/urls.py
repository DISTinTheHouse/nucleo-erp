from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EmpresaViewSet, CoreDashboardView,
    EmpresaListView, EmpresaCreateView, EmpresaUpdateView, EmpresaDeleteView,
    SucursalListView, SucursalCreateView, SucursalUpdateView, SucursalDeleteView,
    DepartamentoListView, DepartamentoCreateView, DepartamentoUpdateView, DepartamentoDeleteView,
    MonedaListView, MonedaCreateView, MonedaUpdateView, MonedaDeleteView
)

app_name = 'nucleo'

router = DefaultRouter()
router.register(r'empresas', EmpresaViewSet)

urlpatterns = [
    # API
    path('api/v1/nucleo/', include(router.urls)),

    # WEB CORE - DASHBOARD
    path('core/dashboard/', CoreDashboardView.as_view(), name='dashboard'),

    # WEB CORE - EMPRESAS
    path('core/empresas/', EmpresaListView.as_view(), name='empresa_list'),
    path('core/empresas/nueva/', EmpresaCreateView.as_view(), name='empresa_create'),
    path('core/empresas/<pk>/editar/', EmpresaUpdateView.as_view(), name='empresa_update'),
    path('core/empresas/<pk>/eliminar/', EmpresaDeleteView.as_view(), name='empresa_delete'),

    # WEB CORE - SUCURSALES
    path('core/sucursales/', SucursalListView.as_view(), name='sucursal_list'),
    path('core/sucursales/nueva/', SucursalCreateView.as_view(), name='sucursal_create'),
    path('core/sucursales/<pk>/editar/', SucursalUpdateView.as_view(), name='sucursal_update'),
    path('core/sucursales/<pk>/eliminar/', SucursalDeleteView.as_view(), name='sucursal_delete'),

    # WEB CORE - DEPARTAMENTOS
    path('core/departamentos/', DepartamentoListView.as_view(), name='departamento_list'),
    path('core/departamentos/nuevo/', DepartamentoCreateView.as_view(), name='departamento_create'),
    path('core/departamentos/<pk>/editar/', DepartamentoUpdateView.as_view(), name='departamento_update'),
    path('core/departamentos/<pk>/eliminar/', DepartamentoDeleteView.as_view(), name='departamento_delete'),

    # WEB CORE - MONEDAS
    path('core/monedas/', MonedaListView.as_view(), name='moneda_list'),
    path('core/monedas/nueva/', MonedaCreateView.as_view(), name='moneda_create'),
    path('core/monedas/<pk>/editar/', MonedaUpdateView.as_view(), name='moneda_update'),
    path('core/monedas/<pk>/eliminar/', MonedaDeleteView.as_view(), name='moneda_delete'),
]
