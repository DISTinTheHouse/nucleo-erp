from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EmpresaViewSet, SucursalViewSet, DepartamentoViewSet, MonedaViewSet,
    CoreDashboardView, get_sucursales_por_empresa,
    EmpresaListView, EmpresaCreateView, EmpresaUpdateView, EmpresaDeleteView,
    SucursalListView, SucursalCreateView, SucursalUpdateView, SucursalDeleteView,
    DepartamentoListView, DepartamentoCreateView, DepartamentoUpdateView, DepartamentoDeleteView,
    MonedaListView, MonedaCreateView, MonedaUpdateView, MonedaDeleteView
)
from .views_sat import (
    SatRegimenFiscalListView, SatUsoCfdiListView, SatMetodoPagoListView,
    SatFormaPagoListView, SatClaveProdServListView, SatClaveUnidadListView
)
from .api_views import (
    UserEmpresasAPIView, UserSucursalesAPIView, SatCatalogosAPIView, 
    EmpresaSatConfigUpdateView
)

app_name = 'nucleo'

router = DefaultRouter()
router.register(r'empresas', EmpresaViewSet)
router.register(r'sucursales', SucursalViewSet)
router.register(r'departamentos', DepartamentoViewSet)
router.register(r'monedas', MonedaViewSet)

urlpatterns = [
    # API (Router Default)B
    path('api/v1/nucleo/', include(router.urls)),
    
    # API Custom (Next.js)
    path('api/v1/nucleo/mis-empresas/', UserEmpresasAPIView.as_view(), name='api_mis_empresas'),
    path('api/v1/nucleo/mis-sucursales/', UserSucursalesAPIView.as_view(), name='api_mis_sucursales'),
    path('api/v1/nucleo/sat/catalogos/', SatCatalogosAPIView.as_view(), name='api_sat_catalogos'),
    path('api/v1/nucleo/empresas/<int:empresa_id>/config-sat/', EmpresaSatConfigUpdateView.as_view(), name='api_empresa_sat_config'),

    # WEB CORE - DASHBOARD
    path('core/dashboard/', CoreDashboardView.as_view(), name='dashboard'),
    
    # AJAX
    path('ajax/sucursales/<int:empresa_id>/', get_sucursales_por_empresa, name='ajax_sucursales'),

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

    # WEB CORE - CATALOGOS SAT
    path('core/sat/regimenes/', SatRegimenFiscalListView.as_view(), name='sat_regimen_list'),
    path('core/sat/usos-cfdi/', SatUsoCfdiListView.as_view(), name='sat_usocfdi_list'),
    path('core/sat/metodos-pago/', SatMetodoPagoListView.as_view(), name='sat_metodopago_list'),
    path('core/sat/formas-pago/', SatFormaPagoListView.as_view(), name='sat_formapago_list'),
    path('core/sat/prod-serv/', SatClaveProdServListView.as_view(), name='sat_prodserv_list'),
    path('core/sat/unidades/', SatClaveUnidadListView.as_view(), name='sat_unidad_list'),
]
