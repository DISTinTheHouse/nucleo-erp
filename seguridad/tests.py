from django.test import TestCase

from compras.services.orden_compra_view_service import (
    puede_ver_contabilidad as puede_ver_contabilidad_compras,
)
from notificaciones.models import Notificacion
from notificaciones.services.notificacion_service import crear_notificacion_por_rol
from nucleo.models import Empresa
from seguridad.models import Rol, UsuarioRol
from seguridad.role_identity import (
    CLAVE_DEPARTAMENTO_MESA_CONTROL,
    inferir_clave_departamento,
    usuario_tiene_clave_departamento,
)
from usuarios.models import Usuario
from ventas.services.pedido_field_filter_service import (
    puede_ver_contabilidad as puede_ver_contabilidad_ventas,
)


class MesaControlRoleIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(
            codigo="acme-seg", razon_social="ACME Seguridad SA"
        )
        cls.usuario_mesa = Usuario.objects.create(
            username="mesa",
            email="mesa@acme-seg.test",
            empresa=cls.empresa,
        )
        cls.usuario_bloqueado = Usuario.objects.create(
            username="mesa_bloqueado",
            email="mesa-bloqueado@acme-seg.test",
            empresa=cls.empresa,
            estatus=Usuario.Estatus.BLOQUEADO,
        )
        cls.rol_mesa = Rol.objects.create(
            empresa=cls.empresa,
            codigo="MESACONTROL-0002",
            nombre="Mesa-de-control",
            estatus=Rol.Estatus.ACTIVO,
            clave_departamento=None,
        )
        UsuarioRol.objects.create(
            usuario=cls.usuario_mesa,
            rol=cls.rol_mesa,
            empresa=cls.empresa,
        )
        UsuarioRol.objects.create(
            usuario=cls.usuario_bloqueado,
            rol=cls.rol_mesa,
            empresa=cls.empresa,
        )

    def test_infiere_clave_departamento_desde_valores_legacy(self):
        self.assertEqual(
            inferir_clave_departamento("MESACONTROL-0002", "Mesa-de-control"),
            CLAVE_DEPARTAMENTO_MESA_CONTROL,
        )

    def test_save_del_rol_normaliza_clave_departamento(self):
        self.rol_mesa.refresh_from_db()
        self.assertEqual(
            self.rol_mesa.clave_departamento,
            CLAVE_DEPARTAMENTO_MESA_CONTROL,
        )

    def test_usuario_activo_con_rol_mesa_control_es_detectado(self):
        self.assertTrue(
            usuario_tiene_clave_departamento(
                self.usuario_mesa,
                CLAVE_DEPARTAMENTO_MESA_CONTROL,
                empresa=self.empresa,
            )
        )

    def test_usuario_bloqueado_no_cuenta_como_mesa_control(self):
        self.assertFalse(
            usuario_tiene_clave_departamento(
                self.usuario_bloqueado,
                CLAVE_DEPARTAMENTO_MESA_CONTROL,
                empresa=self.empresa,
            )
        )

    def test_notificaciones_por_rol_resuelven_mesa_control_legacy(self):
        total = crear_notificacion_por_rol(
            empresa=self.empresa,
            codigo_rol="MESA-DE-CONTROL",
            clave_departamento=CLAVE_DEPARTAMENTO_MESA_CONTROL,
            titulo="Nueva cotizacion",
            mensaje="Lista para revision",
            modulo="ventas",
            tipo="cotizacion_en_revision",
        )
        self.assertEqual(total, 1)
        self.assertEqual(Notificacion.objects.count(), 1)
        self.assertEqual(Notificacion.objects.get().usuario_id, self.usuario_mesa.pk)

    def test_visibilidad_contable_reconoce_mesa_control(self):
        self.assertTrue(puede_ver_contabilidad_ventas(self.usuario_mesa))
        self.assertTrue(puede_ver_contabilidad_compras(self.usuario_mesa))
