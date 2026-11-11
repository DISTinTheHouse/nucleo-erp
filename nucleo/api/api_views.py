import time

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, viewsets, permissions
from rest_framework.exceptions import ValidationError
from django.db import connections, models, transaction
from ..choices import StatusChoices
from ..models import (
    Empresa, Sucursal, Departamento, Moneda, SerieFolio,
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, 
    SatClaveProdServ, SatClaveUnidad,
    UnidadMedida, Impuesto
)
from .serializers import (
    SatRegimenFiscalSerializer, 
    SatUsoCfdiSerializer, SatMetodoPagoSerializer, SatFormaPagoSerializer,
    SatClaveProdServSerializer, SatClaveUnidadSerializer,
    UnidadMedidaSerializer, ImpuestoSerializer,
    EmpresaSerializer, SucursalSerializer, DepartamentoSerializer, MonedaSerializer, SerieFolioSerializer,
    CambiarEmpresaSerializer, UserEmpresaSerializer,
)
from drf_spectacular.utils import extend_schema
from seguridad.api.api_views import IsSuperUserOrReadOnly
from ..scope import empresas_activas

# --- VIEWSETS (Movidios desde views.py para limpiar arquitectura) ---

class EmpresaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar empresas.
    Cualquier usuario autenticado puede leer; solo el superusuario crea, edita o
    borra (``IsSuperUserOrReadOnly``) (#1046).
    """
    queryset = Empresa.objects.all().order_by('-created_at')
    serializer_class = EmpresaSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        # Superusuario: todas (administra el catálogo de empresas). Resto: solo
        # su empresa activa; sin empresa, nada. Otra empresa -> 404 (#1058).
        user = self.request.user
        qs = super().get_queryset()
        if user.is_superuser:
            return qs
        if user.empresa_id:
            return qs.filter(pk=user.empresa_id)
        return qs.none()

    def perform_create(self, serializer):
        # Al crear una empresa, vincular al usuario creador
        empresa = serializer.save()
        user = self.request.user
        
        # Asignar usuario a la empresa y hacerlo admin
        if not user.empresa:
            user.empresa = empresa
            user.is_admin_empresa = True
            user.save()
            
        # Añadir a la lista de empresas permitidas (M2M)
        user.empresas.add(empresa)
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class SucursalViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar sucursales.
    """
    queryset = Sucursal.objects.all()
    serializer_class = SucursalSerializer
    lookup_field = 'codigo'
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero (requisito de producto para las vistas de
        # lista del frontend); el ``-id`` es solo desempate estable.
        base_qs = self.queryset.order_by('-created_at', '-id_sucursal')
        # Superusuario incluido: solo su empresa activa (#1059).
        if hasattr(user, 'empresa') and user.empresa:
            # Filtrar por empresa del usuario
            qs = base_qs.filter(empresa=user.empresa)
            # Y filtrar por sucursales asignadas al usuario (si no es admin empresa)
            if not user.is_superuser and not getattr(user, 'is_admin_empresa', False):
                 qs = qs.filter(id_sucursal__in=user.sucursales.values_list('id_sucursal', flat=True))
            return qs
        return base_qs.none()

    def perform_create(self, serializer):
        # La empresa sale del servidor, nunca del body (#1059).
        if not self.request.user.empresa_id:
            raise ValidationError({'empresa': 'No tienes una empresa activa.'})
        serializer.save(empresa=self.request.user.empresa)

    def perform_destroy(self, instance): 
        instance.soft_delete()

class DepartamentoViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar departamentos.
    """
    queryset = Departamento.objects.all()
    serializer_class = DepartamentoSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        # Superusuario incluido: solo su empresa activa (#1059).
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class MonedaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar monedas.
    Admite monedas Globales (System) y Privadas (Empresa).
    """
    queryset = Moneda.objects.all()
    serializer_class = MonedaSerializer
    lookup_field = 'codigo_iso'
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # 1. Monedas globales (empresa=None)
        q_global = models.Q(empresa__isnull=True)

        # 2. Monedas de la empresa ACTIVA, superusuario incluido (#1035, EC-798):
        #    ``?empresa_id=`` ya no elige otra empresa.
        if user.empresa:
            q_empresa = models.Q(empresa=user.empresa)
            return self.queryset.filter(q_global | q_empresa)

        return self.queryset.filter(q_global)

    def perform_create(self, serializer):
        user = self.request.user
        # Moneda privada de la empresa activa, superusuario incluido (#1059).
        if not user.empresa_id:
            raise ValidationError({'empresa': 'No tienes una empresa activa.'})
        serializer.save(empresa=user.empresa)

    def perform_destroy(self, instance):
        instance.soft_delete()


class SerieFolioViewSet(viewsets.ModelViewSet):
    """
    API endpoint para gestionar Series y Folios de documentos.
    Filtrado por empresa del usuario.
    """
    queryset = SerieFolio.objects.all()
    serializer_class = SerieFolioSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero; ``-id`` como desempate estable.
        base_qs = self.queryset.order_by('-created_at', '-id_serie_folio')
        if user.empresa:
            # Solo mostrar series de la empresa del usuario
            return base_qs.filter(empresa=user.empresa)

        return base_qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        # Siempre la empresa activa, superusuario incluido (#1059).
        if not user.empresa_id:
            raise ValidationError({'empresa': 'No tienes una empresa activa.'})
        serializer.save(empresa=user.empresa)
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class SatClaveProdServViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para buscar Claves de Producto/Servicio SAT.
    Soporta búsqueda por 'q' (código o descripción).
    """
    queryset = SatClaveProdServ.objects.filter(activo=True)
    serializer_class = SatClaveProdServSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get('q', None)
        if q:
            qs = qs.filter(models.Q(codigo__icontains=q) | models.Q(descripcion__icontains=q))
        return qs

class SatClaveUnidadViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para buscar Claves de Unidad SAT.
    Soporta búsqueda por 'q' (código o descripción).
    """
    queryset = SatClaveUnidad.objects.filter(activo=True)
    serializer_class = SatClaveUnidadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get('q', None)
        if q:
            qs = qs.filter(models.Q(codigo__icontains=q) | models.Q(descripcion__icontains=q))
        return qs

class UnidadMedidaViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para Unidades de Medida (Sistema CORE).
    """
    queryset = UnidadMedida.objects.filter(activo=True)
    serializer_class = UnidadMedidaSerializer
    permission_classes = [permissions.IsAuthenticated]

class ImpuestoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para Impuestos (Sistema CORE).
    """
    queryset = Impuesto.objects.filter(activo=True)
    serializer_class = ImpuestoSerializer
    permission_classes = [permissions.IsAuthenticated]

# --- API VIEWS CUSTOM ---

class HealthzAPIView(APIView):
    """Liveness: el proceso WSGI levantó. No toca la DB a propósito -- debe
    responder rápido y sin depender de nada externo. Para disponibilidad real
    (¿puede este proceso hablar con Postgres?) usa ``ReadyzAPIView``."""
    permission_classes = []

    def get(self, request):
        return Response({"ok": True}, status=200)


class ReadyzAPIView(APIView):
    """Readiness: valida que el backend pueda atender tráfico de verdad,
    no solo que el proceso esté vivo. Pensado para monitoreo externo de
    disponibilidad (uptime checks) -- nada en el pipeline de deploy lo
    consulta hoy.

    Solo prueba la base de datos por ahora: es la única dependencia externa
    dura para servir una request (ver DATABASES en ERP/settings.py). Si se
    agregan otras dependencias críticas (cache, servicio externo síncrono),
    se agregan aquí como una entrada más de ``checks``, no un endpoint nuevo.
    """
    permission_classes = []

    def get(self, request):
        checks = {}
        healthy = True

        start = time.monotonic()
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
            checks["database"] = {
                "ok": True,
                "latency_ms": round((time.monotonic() - start) * 1000, 1),
            }
        except Exception as exc:
            healthy = False
            # Mensaje acotado: no se quiere filtrar credenciales de conexión
            # en un endpoint sin autenticación si la excepción las incluyera.
            checks["database"] = {"ok": False, "error": str(exc)[:200]}

        return Response(
            {"ok": healthy, "checks": checks},
            status=200 if healthy else 503,
        )

class SatCatalogosAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Retorna todos los catálogos del SAT para uso en frontend (cacheable).
        """
        # Regímenes Fiscales
        regimenes = SatRegimenFiscal.objects.filter(activo=True)
        usos = SatUsoCfdi.objects.filter(activo=True)
        metodos = SatMetodoPago.objects.filter(activo=True)
        formas = SatFormaPago.objects.filter(activo=True)

        data = {
            'regimenes_fiscales': SatRegimenFiscalSerializer(regimenes, many=True).data,
            'usos_cfdi': SatUsoCfdiSerializer(usos, many=True).data,
            'metodos_pago': SatMetodoPagoSerializer(metodos, many=True).data,
            'formas_pago': SatFormaPagoSerializer(formas, many=True).data,
        }
        return Response(data)

class UserEmpresasAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=UserEmpresaSerializer(many=True))
    def get(self, request):
        """
        Retorna las empresas a las que el usuario tiene acceso.
        Incluye la empresa activa (FK) y las empresas permitidas (M2M).

        Solo empresas activas, con el mismo criterio que acepta
        ``cambiar-empresa`` (``empresas_activas``, EC-843); la empresa activa del
        usuario sale siempre, aunque esté inactiva, para que el front muestre
        dónde está.
        """
        user = request.user
        activas = empresas_activas()
        if not user.is_superuser:
            activas = activas.filter(pk__in=user.empresas.values('pk'))
        empresas = (
            (activas | Empresa.objects.filter(pk=user.empresa_id))
            .distinct().order_by('razon_social', 'pk')
        )
        
        # Usamos el serializer existente o construimos data simple
        data = []
        for emp in empresas:
            data.append({
                'id': emp.pk, # ID interno (UUID o AutoField)
                'codigo': emp.codigo,
                'razon_social': emp.razon_social,
                'nombre_comercial': emp.nombre_comercial,
                'rfc': emp.rfc,
                'logo': emp.logo_url
            })
        
        return Response(data)

class SoloSuperusuario(permissions.BasePermission):
    """Solo el superusuario (EC-843). El M2M ``empresas`` no concede nada aquí."""

    message = "Solo un superusuario puede cambiar de empresa activa."

    def has_permission(self, request, view):
        return bool(getattr(request.user, "is_superuser", False))


class CambiarEmpresaAPIView(APIView):
    """``POST /api/v1/nucleo/cambiar-empresa/`` con ``{"empresa": <id>}`` (EC-843).

    El superusuario cambia **su propia** empresa activa (``user.empresa``) a
    cualquier empresa activa. Todo el ERP lee y escribe solo en la empresa activa
    (EC-798, EC-816); este es el único camino para cambiarla: ni el perfil
    (``/api/auth/user/``, EC-844), ni ``/api/v1/usuarios/``, ni el Web Core
    (``/core/usuarios/<id>/editar/``), ni el admin de Django mueven al propio
    usuario.

    El alcance por sucursal se rehace para cumplir la regla de EC-844
    (``usuarios/alcance.py``): ``sucursal_default`` pasa a la primera sucursal
    activa de la empresa nueva por ``codigo`` (``None`` si no tiene) y
    ``sucursales``/``departamentos`` quedan vacíos; el superusuario no se acota
    por sucursal, así que no pierde lectura. No toca el M2M ``empresas``, los
    roles, los overrides ni ``is_admin_empresa``.

    Pedir la empresa que ya está activa no cambia nada (200, misma respuesta),
    aunque esté inactiva. Cualquier otro valor que no sea una empresa activa
    (``null``, sin ``empresa``, texto, inexistente, inactiva) responde el mismo
    400 ``{"empresa": ["Empresa no válida."]}``.
    Responde lo mismo que ``GET /api/auth/user/`` para que el front actualice la
    sesión sin volver a iniciarla. El JWT solo lleva ``user_id`` y el usuario se
    lee de la BD en cada petición: la siguiente petición ya ve la empresa nueva.
    """

    permission_classes = [IsAuthenticated, SoloSuperusuario]

    def post(self, request):
        from auth_kit.app_settings import auth_kit_settings
        from auditoria.models import AuditoriaEvento
        from nucleo.middleware import ip_auditoria
        from usuarios.models import Usuario

        entrada = CambiarEmpresaSerializer(data=request.data)
        empresa_id = entrada.validated_data.get("empresa") if entrada.is_valid() else None

        with transaction.atomic():
            # Una sola decisión, sobre la fila bloqueada y no sobre ``request.user``
            # (leído al autenticar): si otra petición cambió la empresa entretanto,
            # manda la BD. La empresa actual es no-op aunque esté inactiva; lo
            # demás tiene que ser una empresa activa. Todo lo inválido (``null``,
            # sin ``empresa``, texto, inexistente, inactiva) sale por el mismo 400.
            user = Usuario.objects.select_for_update().get(pk=request.user.pk)
            if empresa_id is None or empresa_id != user.empresa_id:
                empresa = (
                    empresas_activas().filter(pk=empresa_id).first()
                    if empresa_id is not None else None
                )
                if empresa is None:
                    raise ValidationError({"empresa": [CambiarEmpresaSerializer.MENSAJE]})

                SucursalesUsuario = Usuario.sucursales.through
                DepartamentosUsuario = Usuario.departamentos.through
                sucursales = SucursalesUsuario.objects.filter(usuario_id=user.pk)
                departamentos = DepartamentosUsuario.objects.filter(usuario_id=user.pk)
                antes = {
                    "empresa": user.empresa_id,
                    "sucursal_default": user.sucursal_default_id,
                    "sucursales": sorted(sucursales.values_list("sucursal_id", flat=True)),
                    "departamentos": sorted(departamentos.values_list("departamento_id", flat=True)),
                }
                user.empresa = empresa
                user.sucursal_default = (
                    Sucursal.objects.filter(empresa=empresa, activo=True)
                    .order_by("codigo", "pk").first()
                )
                user.save(update_fields=["empresa", "sucursal_default", "updated_at"])
                # Directo sobre las tablas intermedias y solo si había filas (ya se
                # leyeron arriba): ``clear()`` lanza su DELETE aunque no haya nada.
                # No hay receptores de ``m2m_changed`` que se salten.
                if antes["sucursales"]:
                    sucursales.delete()
                if antes["departamentos"]:
                    departamentos.delete()
                AuditoriaEvento.objects.create(
                    empresa=empresa, usuario=user, modulo="usuarios", accion="CAMBIAR_EMPRESA",
                    tabla=Usuario._meta.db_table, id_registro=str(user.pk),
                    antes_json=antes,
                    despues_json={
                        "empresa": empresa.pk, "sucursal_default": user.sucursal_default_id,
                        "sucursales": [], "departamentos": [],
                    },
                    ip=ip_auditoria(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:200],
                )

        perfil = auth_kit_settings.USER_SERIALIZER(user, context={"request": request})
        return Response(perfil.data)


class UserSucursalesAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, empresa_id=None):
        """
        Retorna las sucursales permitidas para el usuario dentro de una empresa.
        Filtra por:
        1. La empresa seleccionada.
        2. Las sucursales asignadas al usuario (M2M).
        """
        user = request.user
        
        # Si no se pasa empresa_id en la URL, intentamos query param o usamos la del usuario
        if not empresa_id:
            empresa_id = request.query_params.get('empresa_id')
            
        if not empresa_id and user.empresa:
            empresa_id = user.empresa.pk

        if not empresa_id:
            return Response({'error': 'Empresa ID es requerido'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            empresa_id = int(empresa_id)
        except (TypeError, ValueError):
            return Response({'error': 'Empresa ID inválido'}, status=status.HTTP_400_BAD_REQUEST)

        # Solo la empresa activa, superusuario incluido (#1035, EC-798): pedir
        # otra empresa (URL o ``?empresa_id=``) responde 403.
        if empresa_id != user.empresa_id:
            return Response({'error': 'No autorizado'}, status=status.HTTP_403_FORBIDDEN)

        # Filtrar sucursales
        # Si es superuser, todas las de la empresa.
        # Si es usuario normal, intersección de (Sucursales de la Empresa) y (Sucursales Asignadas).
         
        active_values = {StatusChoices.ACTIVE, "activo", "ACTIVO"}
        try:
            active_values.add(Sucursal.Estatus.ACTIVO)
        except Exception:
            pass

        qs = Sucursal.objects.filter(empresa_id=empresa_id, activo=True)
        
        if not user.is_superuser and not getattr(user, 'is_admin_empresa', False):
            allowed_ids = list(user.sucursales.values_list('id_sucursal', flat=True))
            if not allowed_ids and getattr(user, 'sucursal_default_id', None):
                try:
                    if user.sucursal_default and user.sucursal_default.empresa_id == empresa_id:
                        allowed_ids = [user.sucursal_default_id]
                except Exception:
                    allowed_ids = allowed_ids
            if allowed_ids:
                qs = qs.filter(id_sucursal__in=allowed_ids)
            else:
                qs = qs.none()

        data = []
        for suc in qs:
            data.append({
                'id': suc.pk,
                'codigo': suc.codigo,
                'nombre': suc.nombre,
                'direccion': suc.direccion_linea1,
                'telefono': suc.telefono
            })

        return Response(data)
