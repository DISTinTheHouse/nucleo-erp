from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AlertaMoraViewSet,
    BancoViewSet,
    CentroCostoViewSet,
    ClienteViewSetContabilidad,
    CobroViewSet,
    ConciliacionBancariaViewSet,
    CuentaBancariaViewSet,
    CuentaContableViewSet,
    CuentaPorCobrarViewSet,
    CuentaPorPagarViewSet,
    DashboardFinancieroViewSet,
    FacturaProveedorViewSet,
    FacturaViewSet,
    MovimientoBancarioViewSet,
    NotaCreditoViewSet,
    PagoViewSet,
    PolizaViewSet,
)

router = DefaultRouter()
router.register(r'bancos', BancoViewSet, basename='banco')
router.register(r'cuentas-bancarias', CuentaBancariaViewSet, basename='cuenta-bancaria')
router.register(r'cuentas-contables', CuentaContableViewSet, basename='cuenta-contable')
router.register(r'cuentas-por-cobrar', CuentaPorCobrarViewSet, basename='cuenta-por-cobrar')
router.register(r'clientes-contabilidad', ClienteViewSetContabilidad, basename='cliente-contabilidad')
router.register(r'centros-costo', CentroCostoViewSet, basename='centro-costo')
router.register(r'facturas', FacturaViewSet, basename='factura')
router.register(r'polizas', PolizaViewSet, basename='poliza')
router.register(r'facturas-proveedor', FacturaProveedorViewSet, basename='factura-proveedor')
router.register(r'cuentas-por-pagar', CuentaPorPagarViewSet, basename='cuenta-por-pagar')
router.register(r'cobros', CobroViewSet, basename='cobro')
router.register(r'pagos', PagoViewSet, basename='pago')
router.register(r'movimientos-bancarios', MovimientoBancarioViewSet, basename='movimiento-bancario')
router.register(r'conciliaciones-bancarias', ConciliacionBancariaViewSet, basename='conciliacion-bancaria')
router.register(r'notas-credito', NotaCreditoViewSet, basename='nota-credito')
router.register(r'alertas-mora', AlertaMoraViewSet, basename='alerta-mora')
router.register(r'dashboard', DashboardFinancieroViewSet, basename='finanzas-dashboard')

urlpatterns = [
    path('', include(router.urls)),
]
