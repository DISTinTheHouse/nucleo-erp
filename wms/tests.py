"""Pruebas del manejo de colisiones de EPC en etiquetas RFID.

Cubren las dos capas de defensa sobre ``EtiquetaRFIDDetalle.epc`` (``unique=True``
global): el pre-chequeo del serializer (400) y la red de seguridad del service
(409), más el bucle acotado de regeneración para EPC generados por backend.
"""

import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.transaction import TransactionManagementError
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from catalogo.models import Producto, TipoProducto
from nucleo.models import Empresa, Sucursal
from usuarios.models import Usuario
from wms.api.serializers import EtiquetaRFIDCreateSerializer
from wms.models import (
    EtiquetaRFIDDetalle,
    EtiquetaRFIDImpresion,
    LectorRFID,
    RfidLecturaUltima,
    RfidScan,
    hash_token_lector,
)
from wms.services.rfid_label_service import (
    MAX_INTENTOS_EPC,
    EtiquetaRFIDColision409,
    RFIDLabelService,
)


def _tipo_producto():
    """Tipo de producto obligatorio (#871); PT tiene pk=3 como en producción."""
    return TipoProducto.objects.get_or_create(codigo="PT", defaults={"pk": 3, "nombre": "Producto terminado"})[0]


EPC_EXISTENTE = "AAAABBBBCCCCDDDDEEEE0001"


class EtiquetaRFIDBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(
            codigo="test-epc", razon_social="Empresa de pruebas EPC"
        )
        cls.otra_empresa = Empresa.objects.create(
            codigo="test-epc-2", razon_social="Otra empresa"
        )
        cls.producto = Producto.objects.create(
            tipo=_tipo_producto(),
            empresa=cls.empresa, nombre="Playera básica", codigo="PB01"
        )
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTZ", nombre="Matriz"
        )
        # Con ``sucursal_default``: es el usuario operativo normal. Sin ella,
        # ``_resolve_context`` ahora rechaza (ver SucursalObligatoriaTests).
        cls.usuario = Usuario.objects.create(
            username="epc-tester",
            email="epc-tester@example.com",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )

    def _crear_detalle_existente(self, epc=EPC_EXISTENTE, empresa=None):
        """Siembra un EPC ya registrado, colgado de su propia impresión."""
        impresion = EtiquetaRFIDImpresion.objects.create(
            empresa=empresa or self.empresa,
            producto=self.producto,
            cantidad=1,
        )
        return EtiquetaRFIDDetalle.objects.create(
            impresion=impresion, epc=epc, barcode_value="PB01"
        )


class LayerAValidacionSerializerTests(EtiquetaRFIDBaseTestCase):
    """Capa A: el serializer rechaza con 400 antes de tocar la BD."""

    def _serializer(self, etiquetas):
        return EtiquetaRFIDCreateSerializer(
            data={
                "producto": self.producto.pk,
                "cantidad": len(etiquetas),
                "rfid_mode": True,
                "etiquetas": etiquetas,
            }
        )

    def test_epc_ya_existente_en_bd_es_400(self):
        """(a) EPC que ya vive en BD -> 400 con mensaje genérico."""
        self._crear_detalle_existente()

        serializer = self._serializer([{"epc": EPC_EXISTENTE}])

        self.assertFalse(serializer.is_valid())
        self.assertIn("etiquetas", serializer.errors)
        mensaje = str(serializer.errors["etiquetas"][0])
        self.assertEqual(mensaje, "Uno o más códigos EPC ya están registrados.")
        # No debe filtrar cuál EPC chocó ni de qué empresa (fuga cross-tenant).
        self.assertNotIn(EPC_EXISTENTE, mensaje)
        self.assertNotIn(self.empresa.codigo, mensaje)

    def test_epc_de_otra_empresa_tambien_es_400_y_no_delata_al_tenant(self):
        """La constraint es global: choca cross-empresa, pero sin revelarlo."""
        self._crear_detalle_existente(empresa=self.otra_empresa)

        serializer = self._serializer([{"epc": EPC_EXISTENTE}])

        self.assertFalse(serializer.is_valid())
        mensaje = str(serializer.errors["etiquetas"][0])
        self.assertEqual(mensaje, "Uno o más códigos EPC ya están registrados.")
        self.assertNotIn(self.otra_empresa.codigo, mensaje)

    def test_epc_repetido_dentro_del_mismo_payload_es_400(self):
        """(b) Mismo EPC dos veces en un solo envío -> 400."""
        serializer = self._serializer(
            [{"epc": EPC_EXISTENTE}, {"epc": EPC_EXISTENTE}]
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            str(serializer.errors["etiquetas"][0]),
            "El arreglo enviado trae códigos EPC repetidos.",
        )

    def test_normalizacion_evita_saltarse_el_pre_chequeo_con_minusculas(self):
        """El service guarda ``.upper()``: el pre-chequeo debe normalizar igual."""
        self._crear_detalle_existente()

        serializer = self._serializer([{"epc": EPC_EXISTENTE.lower()}])

        self.assertFalse(serializer.is_valid())
        self.assertIn("etiquetas", serializer.errors)

    def test_payload_limpio_pasa_la_validacion(self):
        """(d) Regresión: sin colisiones el serializer valida normal."""
        serializer = self._serializer([{"epc": "1111222233334444AAAA0001"}])

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_con_rfid_mode_apagado_no_se_valida_unicidad(self):
        """Sin rfid_mode no se crean detalles: no hay unicidad que violar.

        Reimprimir etiquetas visuales de tags ya registrados era rechazado con
        400 por un conflicto que nunca habría ocurrido.
        """
        self._crear_detalle_existente()

        serializer = EtiquetaRFIDCreateSerializer(
            data={
                "producto": self.producto.pk,
                "cantidad": 1,
                "rfid_mode": False,
                "etiquetas": [{"epc": EPC_EXISTENTE}],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class GeneracionEPCTests(EtiquetaRFIDBaseTestCase):
    """El presupuesto de 24 hex (96 bits / SGTIN-96) es intocable."""

    def test_epc_generado_mide_24_hex(self):
        generados = RFIDLabelService._generate_epc_list(5, producto=self.producto)

        self.assertEqual(len(generados), 5)
        for row in generados:
            self.assertEqual(len(row["epc"]), 24, row["epc"])
            self.assertRegex(row["epc"], r"^[0-9A-F]{24}$")

    def test_epc_con_cantidad_maxima_sigue_en_24_hex(self):
        """``cantidad`` topa en 10000 (0x2710): el idx de 4 hex no se desborda."""
        generados = RFIDLabelService._generate_epc_list(10000, producto=self.producto)

        self.assertEqual(len(generados[-1]["epc"]), 24)

    def test_zpl_rfid_escribe_el_epc_completo(self):
        """El chip se graba en hex (``^RFW,H,,,E``) con el EPC íntegro, una sola vez."""
        epc = RFIDLabelService._generate_epc_list(1, producto=self.producto)[0]["epc"]

        zpl = RFIDLabelService._build_zpl_rfid(epc, producto=self.producto)

        self.assertIn("^RS8\n", zpl)
        self.assertIn(f"^RFW,H,,,E^FD{epc}^FS", zpl)
        self.assertEqual(zpl.count("^RFW"), 1)
        # ``^RFW,E`` (formato por particiones) dejaba el EPC de fábrica.
        self.assertNotIn("^RFW,E", zpl)
        self.assertNotIn("^RB96", zpl)

    def test_zpl_viejo_se_corrige_para_reimprimir(self):
        """El historial reimprime ``zpl_enviado``: el bloque viejo se reemplaza."""
        from wms.services.rfid_label_service import zpl_rfid_vigente

        epc = "000015D80438000188F0D54F"
        viejo = "\n".join([
            "^XA", "^FO40,245^BCN,90,Y,N,N^FD6000100M^FS",
            "^RB96,,,1", "^RS8,E", "^RFW,E", f"^FD{epc}^FS",
            "^RB96,,,1", "^RS8,E", "^RFW,E", f"^FD{epc}^FS",
            "^RB96,,,1", "^RS8,E", "^HV1,3,E,0",
            f"^FO40,360^A0N,18,18^FDEPC: {epc[:16]}...{epc[-8:]}^FS", "^XZ",
        ])

        nuevo = zpl_rfid_vigente(viejo)

        self.assertEqual(nuevo, "\n".join([
            "^XA", "^FO40,245^BCN,90,Y,N,N^FD6000100M^FS",
            "^RS8", f"^RFW,H,,,E^FD{epc}^FS",
            f"^FO40,360^A0N,18,18^FDEPC: {epc[:16]}...{epc[-8:]}^FS", "^XZ",
        ]))
        self.assertEqual(zpl_rfid_vigente(nuevo), nuevo)


class LayerBServiceColisionTests(EtiquetaRFIDBaseTestCase):
    """Capa B: la carrera y la regeneración se traducen a 409, nunca a 500."""

    def _data(self, etiquetas=None, cantidad=1):
        data = {
            "producto": self.producto,
            "producto_variante": None,
            "cantidad": cantidad,
            "rfid_mode": True,
            "status": EtiquetaRFIDImpresion.Estatus.EXITO,
        }
        if etiquetas is not None:
            data["etiquetas"] = etiquetas
        return data

    def test_carrera_con_epc_de_cliente_devuelve_409_no_integrityerror(self):
        """Saltándose el serializer (simula la carrera) se obtiene 409 limpio."""
        self._crear_detalle_existente()

        with self.assertRaises(EtiquetaRFIDColision409) as ctx:
            RFIDLabelService.store_impresion(
                self._data(etiquetas=[{"epc": EPC_EXISTENTE}]), self.usuario
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertNotIn(EPC_EXISTENTE, str(ctx.exception.detail))

    def test_colision_generada_reintenta_y_termina_exitosa(self):
        """(c) Prueba clave del savepoint anidado.

        Fuerza colisión en el intento 1 y éxito en el 2. Si el ``bulk_create`` no
        estuviera envuelto en su propio ``transaction.atomic()``, al atrapar el
        ``IntegrityError`` la transacción externa quedaría ``needs_rollback`` y
        este segundo intento reventaría con ``TransactionManagementError`` en vez
        de completar. Que esta prueba pase ES la evidencia de que el savepoint
        funciona.
        """
        self._crear_detalle_existente()
        epc_bueno = "9999888877776666555500AA"

        respuestas = [
            [{"n": 1, "epc": EPC_EXISTENTE, "serial": "0001"}],
            [{"n": 1, "epc": epc_bueno, "serial": "0001"}],
        ]
        with patch.object(
            RFIDLabelService, "_generate_epc_list", side_effect=respuestas
        ) as mock_gen:
            impresion = RFIDLabelService.store_impresion(self._data(), self.usuario)

        self.assertEqual(mock_gen.call_count, 2)
        detalles = list(impresion.etiquetas.all())
        self.assertEqual(len(detalles), 1)
        self.assertEqual(detalles[0].epc, epc_bueno)
        # El encabezado sobrevivió al intento fallido: el savepoint sólo deshizo
        # el bulk_create, no el create() del header.
        self.assertTrue(
            EtiquetaRFIDImpresion.objects.filter(pk=impresion.pk).exists()
        )

    def test_colision_generada_persistente_agota_intentos_y_lanza_409(self):
        """(c) Tras ``MAX_INTENTOS_EPC`` fallos -> 409, no TransactionManagementError."""
        self._crear_detalle_existente()
        colision = [{"n": 1, "epc": EPC_EXISTENTE, "serial": "0001"}]

        with patch.object(
            RFIDLabelService, "_generate_epc_list", return_value=colision
        ) as mock_gen:
            with self.assertRaises(EtiquetaRFIDColision409) as ctx:
                RFIDLabelService.store_impresion(self._data(), self.usuario)

        self.assertEqual(mock_gen.call_count, MAX_INTENTOS_EPC)
        self.assertEqual(ctx.exception.status_code, 409)
        # Mensaje distinto al de la colisión del cliente, para distinguirlas en logs.
        self.assertIn("intentos", str(ctx.exception.detail))

    def test_colision_generada_deja_la_transaccion_usable(self):
        """El rollback del 409 no debe dejar la conexión rota ni datos huérfanos."""
        self._crear_detalle_existente()
        colision = [{"n": 1, "epc": EPC_EXISTENTE, "serial": "0001"}]

        with patch.object(
            RFIDLabelService, "_generate_epc_list", return_value=colision
        ):
            with self.assertRaises(EtiquetaRFIDColision409):
                RFIDLabelService.store_impresion(self._data(), self.usuario)

        # Si la transacción hubiera quedado marcada, este query reventaría.
        self.assertEqual(
            EtiquetaRFIDDetalle.objects.filter(epc=EPC_EXISTENTE).count(), 1
        )


class SucursalObligatoriaTests(EtiquetaRFIDBaseTestCase):
    """Un no-staff sin ``sucursal_default`` no debe poder crear la impresión.

    Antes se guardaba con ``sucursal=NULL`` y el autor no volvía a verla: el
    ``get_queryset`` del viewset filtra con ``sucursal_id__in=...`` y en SQL un
    ``IN`` nunca matchea ``NULL``, así que ``list`` la omitía y ``retrieve``
    devolvía 404 pese al 201 inicial.
    """

    def _data(self):
        return {
            "producto": self.producto,
            "producto_variante": None,
            "cantidad": 1,
            "rfid_mode": True,
            "status": EtiquetaRFIDImpresion.Estatus.EXITO,
        }

    def _usuario(self, **kwargs):
        base = {
            "username": f"u-{kwargs.get('email', 'x')}",
            "empresa": self.empresa,
            "sucursal_default": None,
        }
        base.update(kwargs)
        return Usuario.objects.create(**base)

    def test_no_staff_sin_sucursal_default_es_rechazado(self):
        """(a) Error claro y accionable, no un éxito silencioso con NULL."""
        user = self._usuario(email="sin-sucursal@example.com")

        with self.assertRaises(ValidationError) as ctx:
            RFIDLabelService.store_impresion(self._data(), user)

        self.assertIn("sucursal", str(ctx.exception.detail).lower())
        # Y sobre todo: no quedó ninguna fila huérfana.
        self.assertEqual(
            EtiquetaRFIDImpresion.objects.filter(sucursal__isnull=True).count(), 0
        )

    def test_superuser_sin_sucursal_default_tambien_es_rechazado(self):
        """El staff no está exento: sus filas NULL eran igual de invisibles.

        Un admin sin ``sucursal_default`` generaba impresiones que ningún
        operador de piso podía ver en ``list``. La exigencia aplica a todos.
        """
        user = self._usuario(email="root@example.com", is_superuser=True)

        with self.assertRaises(ValidationError):
            RFIDLabelService.store_impresion(self._data(), user)

        self.assertEqual(
            EtiquetaRFIDImpresion.objects.filter(sucursal__isnull=True).count(), 0
        )

    def test_admin_empresa_sin_sucursal_default_tambien_es_rechazado(self):
        user = self._usuario(email="admin@example.com", is_admin_empresa=True)

        with self.assertRaises(ValidationError):
            RFIDLabelService.store_impresion(self._data(), user)

    def test_staff_con_sucursal_default_crea_normalmente(self):
        """Lo que el staff conserva es saltarse la pertenencia, no la sucursal."""
        user = self._usuario(
            email="root2@example.com",
            is_superuser=True,
            sucursal_default=self.sucursal,
        )

        impresion = RFIDLabelService.store_impresion(self._data(), user)

        self.assertEqual(impresion.sucursal_id, self.sucursal.pk)

    def test_preview_no_exige_sucursal(self):
        """El preview no escribe nada: no debe bloquearse por falta de sucursal.

        ``_resolve_context`` lo comparten la escritura y el preview; meter ahí la
        exigencia sin condicionarla rompía ``GET /preview/`` para un usuario sin
        ``sucursal_default``, que antes respondía 200 con ``sucursal: null``.
        """
        user = self._usuario(email="preview@example.com")

        payload = RFIDLabelService.onboarding_preview(
            user, producto_id=self.producto.pk, cantidad=2
        )

        self.assertIsNone(payload["sucursal"])
        self.assertEqual(len(payload["etiquetas"]), 2)
        self.assertTrue(payload["zpl_rfid_first"])

    def test_usuario_normal_con_sucursal_default_no_cambia(self):
        """(c) Regresión: el flujo que ya funcionaba sigue igual."""
        impresion = RFIDLabelService.store_impresion(self._data(), self.usuario)

        self.assertEqual(impresion.sucursal_id, self.sucursal.pk)

    def test_la_impresion_creada_es_visible_para_su_autor(self):
        """El bug de origen: lo creado debe pasar el filtro de lectura."""
        impresion = RFIDLabelService.store_impresion(self._data(), self.usuario)

        visibles = EtiquetaRFIDImpresion.objects.filter(
            empresa=self.empresa,
            sucursal_id__in=self.usuario.sucursales_permitidas(),
        )

        self.assertIn(impresion, visibles)


class SavepointEsObligatorioTests(EtiquetaRFIDBaseTestCase):
    """Prueba A/B de por qué el ``atomic`` anidado del retry no es cosmético.

    Aísla el mecanismo puro de Django (``needs_rollback`` en
    ``django/db/transaction.py``) para dejar constancia del modo de fallo. Si
    alguien quita el ``with transaction.atomic()`` del bucle de reintentos en
    ``store_impresion``, el bucle se comporta como el caso negativo de abajo.
    """

    def test_sin_savepoint_la_transaccion_queda_rota(self):
        """Caso negativo: atrapar IntegrityError sin savepoint rompe la TX."""
        detalle = self._crear_detalle_existente()

        with self.assertRaises(TransactionManagementError):
            with transaction.atomic():
                try:
                    # Sin ``atomic`` anidado: no se abre savepoint.
                    EtiquetaRFIDDetalle.objects.create(
                        impresion=detalle.impresion,
                        epc=EPC_EXISTENTE,
                        barcode_value="X",
                    )
                except IntegrityError:
                    pass
                # Es este query posterior el que revienta, no el insert.
                EtiquetaRFIDDetalle.objects.count()

    def test_con_savepoint_la_transaccion_sigue_usable(self):
        """Caso positivo: el mismo flujo con savepoint permite continuar."""
        detalle = self._crear_detalle_existente()

        with transaction.atomic():
            try:
                with transaction.atomic():
                    EtiquetaRFIDDetalle.objects.create(
                        impresion=detalle.impresion,
                        epc=EPC_EXISTENTE,
                        barcode_value="X",
                    )
            except IntegrityError:
                pass
            # Con savepoint el query posterior sí corre: es exactamente lo que
            # permite que el intento 2 del bucle de regeneración exista.
            self.assertEqual(EtiquetaRFIDDetalle.objects.count(), 1)


class RegresionFlujoFelizTests(EtiquetaRFIDBaseTestCase):
    """(d) Sin colisiones, todo debe comportarse exactamente como antes."""

    def test_store_impresion_con_epc_generados(self):
        impresion = RFIDLabelService.store_impresion(
            {
                "producto": self.producto,
                "producto_variante": None,
                "cantidad": 3,
                "rfid_mode": True,
                "status": EtiquetaRFIDImpresion.Estatus.EXITO,
            },
            self.usuario,
        )

        detalles = list(impresion.etiquetas.all())
        self.assertEqual(impresion.cantidad, 3)
        self.assertEqual(len(detalles), 3)
        self.assertEqual(len({d.epc for d in detalles}), 3)
        for detalle in detalles:
            self.assertEqual(len(detalle.epc), 24)
            self.assertEqual(detalle.estado, EtiquetaRFIDDetalle.Estado.IMPRESO)

    def test_store_impresion_con_epc_del_cliente(self):
        etiquetas = [
            {"epc": "1111222233334444AAAA0001", "serial": "0001"},
            {"epc": "1111222233334444AAAA0002", "serial": "0002"},
        ]

        impresion = RFIDLabelService.store_impresion(
            {
                "producto": self.producto,
                "producto_variante": None,
                "cantidad": 2,
                "rfid_mode": True,
                "etiquetas": etiquetas,
                "status": EtiquetaRFIDImpresion.Estatus.PENDIENTE,
            },
            self.usuario,
        )

        detalles = list(impresion.etiquetas.order_by("epc"))
        self.assertEqual([d.epc for d in detalles], [e["epc"] for e in etiquetas])
        self.assertEqual(detalles[0].estado, EtiquetaRFIDDetalle.Estado.PENDIENTE)

    def test_rfid_mode_apagado_no_crea_detalles(self):
        impresion = RFIDLabelService.store_impresion(
            {
                "producto": self.producto,
                "producto_variante": None,
                "cantidad": 2,
                "rfid_mode": False,
            },
            self.usuario,
        )

        self.assertEqual(impresion.etiquetas.count(), 0)


class ScannerRFIDSeguridadTests(TestCase):
    """Webhook con token por lector y lecturas aisladas por empresa."""

    RECEIVE_URL = "/api/v1/wms/etiquetas-rfid/scans/receive/"
    SCANS_URL = "/api/v1/wms/etiquetas-rfid/scans/"
    CLEAR_URL = "/api/v1/wms/etiquetas-rfid/scans/clear/"
    STATS_URL = "/api/v1/wms/etiquetas-rfid/scanner-stats/"
    # ``scans/clear`` responde también los filtros aplicados (#1455).
    SIN_FILTROS = {"lector": None, "hasta_id": None}

    @classmethod
    def setUpTestData(cls):
        cls.empresa_a = Empresa.objects.create(codigo="rfid-a", razon_social="Empresa A")
        cls.empresa_b = Empresa.objects.create(codigo="rfid-b", razon_social="Empresa B")
        cls.sucursal_a = Sucursal.objects.create(empresa=cls.empresa_a, codigo="A1", nombre="A1")
        cls.sucursal_a2 = Sucursal.objects.create(empresa=cls.empresa_a, codigo="A2", nombre="A2")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.empresa_b, codigo="B1", nombre="B1")
        cls.lector_a = LectorRFID.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a, nombre="FX A"
        )
        cls.lector_a2 = LectorRFID.objects.create(
            empresa=cls.empresa_a, sucursal=cls.sucursal_a2, nombre="FX A2"
        )
        cls.lector_b = LectorRFID.objects.create(
            empresa=cls.empresa_b, sucursal=cls.sucursal_b, nombre="FX B"
        )
        cls.usuario_a = Usuario.objects.create(
            username="scan-a",
            email="scan-a@example.com",
            empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a,
        )
        cls.usuario_sin_sucursales = Usuario.objects.create(
            username="scan-a-sin-suc", email="scan-a-sin-suc@example.com", empresa=cls.empresa_a
        )
        cls.usuario_sin_empresa = Usuario.objects.create(
            username="scan-sin-emp", email="scan-sin-emp@example.com"
        )
        cls.admin_a = Usuario.objects.create(
            username="scan-admin-a",
            email="scan-admin-a@example.com",
            empresa=cls.empresa_a,
            is_admin_empresa=True,
        )
        # EC-884: el superusuario se acota a su empresa activa (A), como en EC-882.
        cls.superuser = Usuario.objects.create(
            username="scan-root",
            email="scan-root@example.com",
            is_superuser=True,
            empresa=cls.empresa_a,
        )

    def _post_receive(self, payload=None, **extra):
        return self.client.post(
            self.RECEIVE_URL,
            data=json.dumps(payload or [{"epcId": "AAAABBBBCCCCDDDDEEEE0001"}]),
            content_type="application/json",
            **extra,
        )

    def _sembrar(self, empresa, lector, n, prefijo):
        RfidScan.objects.bulk_create(
            [RfidScan(empresa=empresa, lector=lector, epc=f"{prefijo}{i:08d}") for i in range(n)]
        )

    def _api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    # ── Webhook ───────────────────────────────────────────────────────

    def test_receive_sin_token_401(self):
        self.assertEqual(self._post_receive().status_code, 401)
        self.assertEqual(RfidScan.objects.count(), 0)

    def test_receive_token_invalido_401(self):
        resp = self._post_receive(HTTP_X_RFID_TOKEN="no-existe")
        self.assertEqual(resp.status_code, 401)

    def test_receive_lector_inactivo_401(self):
        LectorRFID.objects.filter(pk=self.lector_a.pk).update(activo=False)
        resp = self._post_receive(HTTP_X_RFID_TOKEN=self.lector_a.token)
        self.assertEqual(resp.status_code, 401)

    def test_receive_header_liga_empresa_y_lector(self):
        resp = self._post_receive(HTTP_X_RFID_TOKEN=self.lector_a.token)
        self.assertEqual(resp.status_code, 200)
        scan = RfidScan.objects.get()
        self.assertEqual(scan.empresa_id, self.empresa_a.pk)
        self.assertEqual(scan.lector_id, self.lector_a.pk)
        self.lector_a.refresh_from_db()
        self.assertIsNotNone(self.lector_a.ultima_lectura)

    def test_receive_token_en_query_string(self):
        self.client.post(
            f"{self.RECEIVE_URL}?token={self.lector_b.token}",
            data=json.dumps([{"epcId": "AAAABBBBCCCCDDDDEEEE0002"}]),
            content_type="application/json",
        )
        self.assertEqual(RfidScan.objects.get().empresa_id, self.empresa_b.pk)

    def test_receive_bearer(self):
        resp = self._post_receive(HTTP_AUTHORIZATION=f"Bearer {self.lector_a.token}")
        self.assertEqual(resp.status_code, 200)

    # ── API ───────────────────────────────────────────────────────────

    def test_scans_filtra_por_empresa_antes_del_limite(self):
        self._sembrar(self.empresa_a, self.lector_a, 3, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 60, "bbbb")

        resp = self._api(self.usuario_a).get(self.SCANS_URL)

        self.assertEqual(resp.status_code, 200)
        epcs = [s["epc"] for s in resp.json()["scans"]]
        self.assertEqual(len(epcs), 3)
        self.assertTrue(all(e.startswith("aaaa") for e in epcs))

    # ── Cursor ``despues_de_id`` (EC-982) ─────────────────────────────

    def _ids_scans(self, user, **params):
        resp = self._api(user).get(self.SCANS_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return [s["id"] for s in resp.json()["scans"]]

    def test_cursor_solo_lecturas_con_id_mayor(self):
        self._sembrar(self.empresa_a, self.lector_a, 3, "viej")
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._sembrar(self.empresa_a, self.lector_a, 2, "nuev")
        nuevas = list(RfidScan.objects.filter(pk__gt=corte).order_by("id").values_list("id", flat=True))

        # Con cursor, por ``id`` ascendente (EC-984).
        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=corte), nuevas)
        # Sin cursor, igual que antes: las cinco.
        self.assertEqual(len(self._ids_scans(self.admin_a)), 5)
        # Cursor en 0 = sin recorte; cursor más allá de todo = vacío.
        self.assertEqual(len(self._ids_scans(self.admin_a, despues_de_id=0)), 5)
        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=nuevas[-1]), [])

    def test_cursor_con_limite_toma_las_mas_viejas_despues_del_corte(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "viej")
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._sembrar(self.empresa_a, self.lector_a, 5, "nuev")
        nuevas = list(RfidScan.objects.filter(pk__gt=corte).order_by("id").values_list("id", flat=True))

        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=corte, limite=3), nuevas[:3])

    # ── Cursor ascendente: polls consecutivos sin saltos (EC-984) ─────

    def _recibir(self, epcs, lector=None):
        """Lote por ``scans/receive/`` (llena ``RfidLecturaUltima``); envejece
        ``visto`` antes para pasar la ventana de dedupe de 30 s."""
        from datetime import timedelta

        from django.utils import timezone

        RfidLecturaUltima.objects.update(visto=timezone.now() - timedelta(seconds=31))
        lector = lector or self.lector_a
        resp = self._post_receive([{"epcId": e} for e in epcs], HTTP_X_RFID_TOKEN=lector.token)
        self.assertEqual(resp.status_code, 200, resp.content)

    def _recorrer(self, user, cursor, **params):
        """Pollea como el frontend: el cursor avanza al ``id`` más alto de cada
        respuesta, hasta una respuesta vacía. Devuelve los ids de cada poll."""
        polls = []
        for _ in range(20):
            ids = self._ids_scans(user, despues_de_id=cursor, **params)
            if not ids:
                return polls
            self.assertEqual(ids, sorted(ids))
            polls.append(ids)
            cursor = max(ids)
        self.fail("El recorrido no terminó")

    def test_cursor_recorre_mas_lecturas_que_el_limite(self):
        self._recibir(["0000AAAA%08d" % i for i in range(2)])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        # Una tarima: 7 lecturas de golpe, con ``limite=3`` entre dos polls.
        self._recibir(["1111BBBB%08d" % i for i in range(7)])
        esperadas = list(RfidScan.objects.filter(pk__gt=corte).order_by("id").values_list("id", flat=True))
        self.assertEqual(len(esperadas), 7)

        polls = self._recorrer(self.admin_a, corte, limite=3)

        self.assertEqual([len(p) for p in polls], [3, 3, 1])
        self.assertEqual([i for p in polls for i in p], esperadas)
        # Lo mismo con ``lector``.
        polls = self._recorrer(self.admin_a, corte, limite=3, lector=self.lector_a.pk)
        self.assertEqual([i for p in polls for i in p], esperadas)

    def test_cursor_con_unicos_recorre_todos_los_epc(self):
        self._recibir(["0000AAAA%08d" % i for i in range(2)])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        epcs = ["2222CCCC%08d" % i for i in range(5)]
        self._recibir(epcs)
        # Releídos: la última de cada EPC es la del segundo lote.
        self._recibir(epcs[:3])
        ultimas = sorted(
            RfidLecturaUltima.objects.filter(epc__in=epcs).values_list("scan_id", flat=True)
        )
        self.assertEqual(len(ultimas), 5)

        for extra in ({}, {"lector": self.lector_a.pk}):
            with self.subTest(**extra):
                polls = self._recorrer(self.admin_a, corte, limite=2, unicos=1, **extra)
                self.assertEqual([len(p) for p in polls], [2, 2, 1])
                self.assertEqual([i for p in polls for i in p], ultimas)

    def test_cursor_con_unicos_y_varios_lectores_no_salta_epc(self):
        # Mismo EPC en dos lectores: sin ``lector`` sale una vez (la más reciente).
        self._recibir(["0000AAAA00000000"])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._recibir(["3333DDDD%08d" % i for i in range(4)], lector=self.lector_a)
        self._recibir(["3333DDDD00000000", "3333DDDD00000001"], lector=self.lector_a2)

        polls = self._recorrer(self.admin_a, corte, limite=2, unicos=1)
        epcs = [
            RfidScan.objects.get(pk=i).epc for p in polls for i in p
        ]
        self.assertEqual(sorted(epcs), ["3333DDDD%08d" % i for i in range(4)])
        # Solo el lector A2: sus dos EPC; las de A no se cuelan.
        polls = self._recorrer(self.admin_a, corte, limite=1, unicos=1, lector=self.lector_a2.pk)
        self.assertEqual(
            sorted(RfidScan.objects.get(pk=i).lector_id for p in polls for i in p),
            [self.lector_a2.pk] * 2,
        )

    def test_cursor_recorrido_no_cruza_empresas(self):
        self._recibir(["0000AAAA00000000"])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._recibir(["4444EEEE%08d" % i for i in range(3)], lector=self.lector_b)
        self._recibir(["5555FFFF%08d" % i for i in range(3)])
        self._recibir(["4444EEEE%08d" % i for i in range(3, 6)], lector=self.lector_b)
        propias = list(
            RfidScan.objects.filter(pk__gt=corte, empresa=self.empresa_a).order_by("id").values_list("id", flat=True)
        )

        for user in (self.admin_a, self.superuser):
            for extra in ({}, {"unicos": 1}):
                with self.subTest(user=user.username, **extra):
                    polls = self._recorrer(user, corte, limite=2, **extra)
                    self.assertEqual([i for p in polls for i in p], propias)
        # Lector de otra empresa: 404, también con cursor.
        resp = self._api(self.admin_a).get(
            self.SCANS_URL, {"despues_de_id": corte, "lector": self.lector_b.pk}
        )
        self.assertEqual(resp.status_code, 404)

    def test_sin_cursor_orden_sin_cambios(self):
        from datetime import timedelta

        from django.utils import timezone

        self._sembrar(self.empresa_a, self.lector_a, 5, "orde")
        ids = list(RfidScan.objects.order_by("id").values_list("id", flat=True))
        # ``created_at`` fuera del orden de ``id``: la más vieja por id es la más
        # reciente por fecha, y dos comparten fecha (desempata ``-id``).
        ahora = timezone.now()
        RfidScan.objects.filter(pk=ids[0]).update(created_at=ahora + timedelta(hours=1))
        RfidScan.objects.filter(pk__in=ids[1:3]).update(created_at=ahora - timedelta(hours=1))
        esperadas = [ids[0], ids[4], ids[3], ids[2], ids[1]]
        self.assertEqual(
            list(RfidScan.objects.order_by("-created_at", "-id").values_list("id", flat=True)), esperadas
        )
        self.assertNotEqual(esperadas, sorted(ids, reverse=True))

        self.assertEqual(self._ids_scans(self.admin_a), esperadas)
        self.assertEqual(self._ids_scans(self.admin_a, limite=2), esperadas[:2])
        self.assertEqual(self._ids_scans(self.admin_a, lector=self.lector_a.pk), esperadas)

    def test_cursor_solo_match_recorre_las_etiquetas_impresas(self):
        self._recibir(["0000AAAA00000000"])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        nuestras = [self._etiqueta_a(epc="6666AAAA%08d" % i).epc for i in range(5)]
        ajenas = ["3035C9D3%08d" % i for i in range(4)]
        # Intercaladas: etiqueta, ajena, etiqueta...
        self._recibir([e for par in zip(nuestras, ajenas) for e in par] + [nuestras[-1]])
        ligadas = list(
            RfidScan.objects.filter(pk__gt=corte, etiqueta__isnull=False).order_by("id").values_list("id", flat=True)
        )
        self.assertEqual(len(ligadas), 5)

        for extra in ({}, {"unicos": 1}, {"lector": self.lector_a.pk}):
            with self.subTest(**extra):
                polls = self._recorrer(self.admin_a, corte, limite=2, solo_match=1, **extra)
                self.assertEqual([len(p) for p in polls], [2, 2, 1])
                self.assertEqual([i for p in polls for i in p], ligadas)

    def test_cursor_no_admin_recorre_solo_sus_sucursales(self):
        self._recibir(["0000AAAA00000000"])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._recibir(["7777AAAA%08d" % i for i in range(3)], lector=self.lector_a)
        self._recibir(["7777A2A2%08d" % i for i in range(3)], lector=self.lector_a2)
        self._recibir(["7777AAAA%08d" % i for i in range(3, 5)], lector=self.lector_a)
        propias = list(
            RfidScan.objects.filter(pk__gt=corte, lector=self.lector_a).order_by("id").values_list("id", flat=True)
        )
        self.assertEqual(len(propias), 5)

        for extra in ({}, {"unicos": 1}):
            with self.subTest(**extra):
                polls = self._recorrer(self.usuario_a, corte, limite=2, **extra)
                self.assertEqual([len(p) for p in polls], [2, 2, 1])
                self.assertEqual([i for p in polls for i in p], propias)

    def test_unicos_no_admin_no_pierde_epc_leido_luego_en_otra_sucursal(self):
        # EPC X: primero el lector de su sucursal (A), luego el de otra (A2). La
        # última de la empresa es la de A2; un no admin de A debe ver la de A.
        self._recibir(["0000AAAA00000000"])
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        self._recibir(["8888ABCD00000001"], lector=self.lector_a)
        propia = RfidScan.objects.get(epc="8888ABCD00000001", lector=self.lector_a).pk
        self._recibir(["8888ABCD00000001"], lector=self.lector_a2)
        ajena = RfidScan.objects.get(epc="8888ABCD00000001", lector=self.lector_a2).pk

        # Sin cursor y con cursor: sale la de su lector, nunca la de A2.
        for extra in ({}, {"despues_de_id": corte}):
            with self.subTest(**extra):
                ids = self._ids_scans(self.usuario_a, unicos=1, **extra)
                self.assertIn(propia, ids)
                self.assertNotIn(ajena, ids)
        # El admin ve la más reciente de la empresa (la de A2), una sola vez.
        ids_admin = self._ids_scans(self.admin_a, unicos=1, despues_de_id=corte)
        self.assertIn(ajena, ids_admin)
        self.assertNotIn(propia, ids_admin)

    def test_cursor_unicos_elige_la_ultima_por_id(self):
        # ``visto`` invertido respecto al ``id`` (dos receive simultáneos): con
        # cursor manda el ``id``, así la lectura posterior al cursor no se pierde.
        from datetime import timedelta

        from django.utils import timezone

        self._recibir(["9999ABCD00000001"], lector=self.lector_a)
        vieja = RfidScan.objects.get(epc="9999ABCD00000001", lector=self.lector_a).pk
        self._recibir(["9999ABCD00000001"], lector=self.lector_a2)
        nueva = RfidScan.objects.get(epc="9999ABCD00000001", lector=self.lector_a2).pk
        RfidLecturaUltima.objects.filter(scan_id=nueva).update(visto=timezone.now() - timedelta(hours=1))
        RfidLecturaUltima.objects.filter(scan_id=vieja).update(visto=timezone.now())

        self.assertEqual(self._ids_scans(self.admin_a, unicos=1, despues_de_id=vieja), [nueva])
        self.assertEqual(self._ids_scans(self.admin_a, unicos=1, despues_de_id=0), [nueva])
        # Sin cursor, como siempre: la de ``visto`` más reciente.
        self.assertEqual(self._ids_scans(self.admin_a, unicos=1), [vieja])

    def test_cursor_con_unicos_busca_la_ultima_lectura_entre_las_nuevas(self):
        # Por ``scans/receive/`` con el token del lector: así se llena
        # ``RfidLecturaUltima``, de donde sale ``unicos`` (#1460). Entre lotes se
        # envejece ``visto`` para pasar la ventana de dedupe de 30 s.
        from datetime import timedelta

        from django.utils import timezone

        def recibir(*epcs):
            RfidLecturaUltima.objects.update(visto=timezone.now() - timedelta(seconds=31))
            resp = self._post_receive([{"epcId": e} for e in epcs], HTTP_X_RFID_TOKEN=self.lector_a.token)
            self.assertEqual(resp.status_code, 200, resp.content)

        def ultima(epc):
            return RfidScan.objects.filter(epc=epc).order_by("-id").values_list("id", flat=True).first()

        # AAAA antes y después del corte; BBBB solo antes; CCCC solo después.
        recibir("AAAABBBBCCCCDDDD0001", "BBBBCCCCDDDDEEEE0001")
        viejo = ultima("AAAABBBBCCCCDDDD0001")
        corte = RfidScan.objects.order_by("-id").values_list("id", flat=True).first()
        recibir("AAAABBBBCCCCDDDD0001")
        r1 = ultima("AAAABBBBCCCCDDDD0001")
        recibir("AAAABBBBCCCCDDDD0001", "CCCCDDDDEEEEFFFF0001")
        r2, r3 = ultima("AAAABBBBCCCCDDDD0001"), ultima("CCCCDDDDEEEEFFFF0001")
        self.assertEqual(len({viejo, r1, r2, r3}), 4)

        ids = self._ids_scans(self.admin_a, despues_de_id=corte, unicos=1)
        self.assertEqual(sorted(ids), sorted([r2, r3]))
        # Combinado con ``lector``: lo mismo; con otro lector de la empresa, nada.
        self.assertEqual(
            sorted(self._ids_scans(self.admin_a, despues_de_id=corte, unicos=1, lector=self.lector_a.pk)),
            sorted([r2, r3]),
        )
        # Cursor más allá de la última lectura de cada EPC: vacío.
        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=max(r2, r3), unicos=1), [])
        # Con solo_match: solo las ligadas a etiqueta (ninguna aquí).
        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=corte, unicos=1, solo_match=1), [])

    def test_cursor_invalido_400(self):
        for valor in ("x", "-1", "1.5", "99999999999999999999"):
            with self.subTest(valor=valor):
                resp = self._api(self.admin_a).get(self.SCANS_URL, {"despues_de_id": valor})
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("despues_de_id", resp.json())
        # Vacío = sin cursor, como los demás filtros enteros del repo.
        self.assertEqual(self._api(self.admin_a).get(self.SCANS_URL, {"despues_de_id": ""}).status_code, 200)

    def test_cursor_no_cruza_empresas(self):
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")
        propia = RfidScan.objects.get(empresa=self.empresa_a).pk

        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=0), [propia])
        self.assertEqual(self._ids_scans(self.superuser, despues_de_id=0), [propia])
        self.assertEqual(self._ids_scans(self.admin_a, despues_de_id=propia), [])

    # ── ``cursor`` explícito (EC-998) ─────────────────────────────────

    def _scans(self, user, **params):
        resp = self._api(user).get(self.SCANS_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _max(self, **filtro):
        return RfidScan.objects.filter(**filtro).order_by("-id").values_list("id", flat=True).first()

    def test_cursor_sin_despues_de_id_es_el_mas_alto_visible(self):
        self._sembrar(self.empresa_a, self.lector_a, 3, "aaaa")
        self._sembrar(self.empresa_a, self.lector_a2, 2, "a2a2")
        self._sembrar(self.empresa_b, self.lector_b, 4, "bbbb")  # ids más altos, de otra empresa
        tope_a = self._max(empresa=self.empresa_a)
        tope_lector_a = self._max(lector=self.lector_a)
        for user in (self.admin_a, self.superuser):
            with self.subTest(user=user.username):
                data = self._scans(user)
                self.assertEqual(set(data), {"scans", "debug_get", "cursor"})
                self.assertEqual(data["cursor"], tope_a)
                # ``solo_match`` vacía ``scans`` pero no mueve el cursor.
                data = self._scans(user, solo_match=1, unicos=1)
                self.assertEqual((data["scans"], data["cursor"]), ([], tope_a))
                self.assertEqual(self._scans(user, lector=self.lector_a.pk)["cursor"], tope_lector_a)
        # No admin: solo los lectores de sus sucursales.
        self.assertEqual(self._scans(self.usuario_a)["cursor"], tope_lector_a)
        self.assertEqual(self._scans(self.usuario_sin_sucursales)["cursor"], 0)

    def test_cursor_con_el_feed_vacio(self):
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")
        for user in (self.admin_a, self.superuser, self.usuario_sin_empresa):
            with self.subTest(user=user.username):
                data = self._scans(user)
                self.assertEqual((data["scans"], data["cursor"]), ([], 0))
        self.assertEqual(self._scans(self.admin_a, despues_de_id=0)["cursor"], 0)

    def test_cursor_con_despues_de_id(self):
        self._sembrar(self.empresa_a, self.lector_a, 3, "viej")
        corte = self._max()
        self._sembrar(self.empresa_a, self.lector_a, 4, "nuev")
        nuevas = list(RfidScan.objects.filter(pk__gt=corte).order_by("id").values_list("id", flat=True))
        self.assertEqual(self._scans(self.admin_a, despues_de_id=corte)["cursor"], nuevas[-1])
        # El más alto devuelto, no el más alto que existe.
        self.assertEqual(self._scans(self.admin_a, despues_de_id=corte, limite=2)["cursor"], nuevas[1])
        # Nada nuevo: el mismo cursor, también con un ``despues_de_id`` mayor que todo.
        self.assertEqual(self._scans(self.admin_a, despues_de_id=nuevas[-1])["cursor"], nuevas[-1])
        self.assertEqual(self._scans(self.admin_a, despues_de_id=nuevas[-1] + 100)["cursor"], nuevas[-1] + 100)
        self.assertEqual(self._scans(self.admin_a, despues_de_id=corte, solo_match=1)["cursor"], corte)

    def test_cursor_deja_fuera_lecturas_viejas_ligadas_despues(self):
        """El caso del feed filtrado: arrancar con el ``id`` más alto de ``scans``
        (vacío con ``solo_match``) dejaba entrar una lectura vieja que después se
        liga a una etiqueta (``ligar_lecturas_previas``)."""
        vieja = RfidScan.objects.create(empresa=self.empresa_a, lector=self.lector_a, epc="EEEE0000000000000000AAAA")
        self._sembrar(self.empresa_a, self.lector_a, 2, "ajen")  # tags ajenos, sin etiqueta
        inicio = self._scans(self.admin_a, solo_match=1)
        self.assertEqual(inicio["scans"], [])
        self.assertGreater(inicio["cursor"], vieja.pk)
        impresion = EtiquetaRFIDImpresion.objects.create(empresa=self.empresa_a, sucursal=self.sucursal_a, cantidad=1)
        detalle = EtiquetaRFIDDetalle.objects.create(impresion=impresion, epc=vieja.epc, barcode_value="X")
        RfidScan.objects.filter(pk=vieja.pk).update(etiqueta=detalle)
        siguiente = self._scans(self.admin_a, solo_match=1, despues_de_id=inicio["cursor"])
        self.assertEqual(siguiente["scans"], [])
        self.assertEqual(self._ids_scans(self.admin_a, solo_match=1, despues_de_id=0), [vieja.pk])

    def test_cursor_se_fija_antes_de_leer_scans(self):
        """Una lectura que llega después de fijar el cursor sale en el siguiente
        polling; una que llegó antes ya viene en la respuesta de arranque. Con el
        cursor calculado después de leer ``scans``, la del medio quedaba debajo
        del cursor sin haber salido nunca."""
        from unittest import mock

        from wms.api.views import EtiquetaRFIDViewSet

        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        original = EtiquetaRFIDViewSet._cursor_de_arranque
        llegadas = {}

        def con_llegadas(user, lector_id):
            llegadas["antes"] = RfidScan.objects.create(empresa=self.empresa_a, lector=self.lector_a, epc="ANTES0000000000000000001")
            cursor = original(user, lector_id)
            llegadas["despues"] = RfidScan.objects.create(empresa=self.empresa_a, lector=self.lector_a, epc="DESPU0000000000000000001")
            return cursor

        with mock.patch.object(EtiquetaRFIDViewSet, "_cursor_de_arranque", staticmethod(con_llegadas)):
            inicio = self._scans(self.admin_a)
        self.assertEqual(inicio["cursor"], llegadas["antes"].pk)
        self.assertIn(llegadas["antes"].pk, [s["id"] for s in inicio["scans"]])
        # La del medio queda arriba del cursor (puede venir también en el arranque,
        # que el cliente descarta): el siguiente polling la trae.
        self.assertGreater(llegadas["despues"].pk, inicio["cursor"])
        siguiente = self._scans(self.admin_a, despues_de_id=inicio["cursor"])
        self.assertEqual([s["id"] for s in siguiente["scans"]], [llegadas["despues"].pk])

    def test_cursor_de_arranque_igual_al_de_visibles_para(self):
        """El cursor de un no admin, partido en sus dos ramas (lectores de sus
        sucursales y lecturas sin lector), es el mismo que el ``id`` más alto de
        ``visibles_para`` para cualquier usuario y filtro de lector."""
        from wms.api.views import EtiquetaRFIDViewSet

        impresiones = {
            suc: EtiquetaRFIDImpresion.objects.create(empresa=self.empresa_a, sucursal=suc, cantidad=1)
            for suc in (self.sucursal_a, self.sucursal_a2)
        }
        etiquetas = {
            suc: EtiquetaRFIDDetalle.objects.create(impresion=imp, epc=f"CAFE{suc.pk:020d}", barcode_value="X")
            for suc, imp in impresiones.items()
        }
        self._sembrar(self.empresa_a, self.lector_a, 3, "aaaa")
        self._sembrar(self.empresa_a, self.lector_a2, 3, "a2a2")
        # Sin lector (sin token): visibles por la sucursal de su etiqueta; la última, de a2.
        RfidScan.objects.create(empresa=self.empresa_a, epc=etiquetas[self.sucursal_a].epc, etiqueta=etiquetas[self.sucursal_a])
        RfidScan.objects.create(empresa=self.empresa_a, epc=etiquetas[self.sucursal_a2].epc, etiqueta=etiquetas[self.sucursal_a2])
        self._sembrar(self.empresa_b, self.lector_b, 2, "bbbb")
        usuarios = (self.admin_a, self.superuser, self.usuario_a, self.usuario_sin_sucursales, self.usuario_sin_empresa)
        for user in usuarios:
            for lector_id in (None, self.lector_a.pk, self.lector_a2.pk, self.lector_b.pk):
                with self.subTest(user=user.username, lector=lector_id):
                    visibles = RfidScan.objects.visibles_para(user)
                    if lector_id is not None:
                        visibles = visibles.filter(lector_id=lector_id)
                    esperado = visibles.order_by("-id").values_list("id", flat=True).first() or 0
                    self.assertEqual(EtiquetaRFIDViewSet._cursor_de_arranque(user, lector_id), esperado)
        # El no admin de la sucursal A: su lector y la lectura sin lector de A, no las de A2.
        self.assertEqual(
            EtiquetaRFIDViewSet._cursor_de_arranque(self.usuario_a, None),
            RfidScan.objects.get(etiqueta=etiquetas[self.sucursal_a]).pk,
        )

    def test_cursor_cuesta_una_consulta_solo_sin_despues_de_id(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._sembrar(self.empresa_a, self.lector_a, 5, "aaaa")
        corte = self._max()
        api = self._api(self.admin_a)
        with CaptureQueriesContext(connection) as sin_cursor:
            api.get(self.SCANS_URL)
        with CaptureQueriesContext(connection) as con_cursor:
            api.get(self.SCANS_URL, {"despues_de_id": corte})
        # Una consulta extra solo sin ``despues_de_id``: la del id más alto visible.
        self.assertEqual(len(sin_cursor), len(con_cursor) + 1)
        tope = [q["sql"] for q in sin_cursor if q["sql"].startswith('SELECT "rfid_scans"."id" AS "id" FROM') and q["sql"].endswith("DESC LIMIT 1")]
        self.assertEqual(len(tope), 1)

    def test_scans_no_admin_solo_ve_lectores_de_sus_sucursales(self):
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        self._sembrar(self.empresa_a, self.lector_a2, 1, "a2a2")

        epcs = [s["epc"] for s in self._api(self.usuario_a).get(self.SCANS_URL).json()["scans"]]

        self.assertEqual(epcs, ["aaaa00000000"])

    def test_scans_admin_ve_toda_su_empresa(self):
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        self._sembrar(self.empresa_a, self.lector_a2, 1, "a2a2")
        self._sembrar(self.empresa_b, self.lector_b, 1, "bbbb")
        resp = self._api(self.admin_a).get(self.SCANS_URL)
        self.assertEqual(len(resp.json()["scans"]), 2)

    def test_scans_sin_empresa_o_sin_sucursales_no_ve_nada(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        for user in (self.usuario_sin_empresa, self.usuario_sin_sucursales):
            with self.subTest(user=user.username):
                resp = self._api(user).get(self.SCANS_URL)
                self.assertEqual(resp.json()["scans"], [])

    def test_scans_match_no_cruza_etiquetas_de_otra_empresa(self):
        impresion_b = EtiquetaRFIDImpresion.objects.create(
            empresa=self.empresa_b, sucursal=self.sucursal_b, cantidad=1
        )
        EtiquetaRFIDDetalle.objects.create(
            impresion=impresion_b, epc="BBBBBBBBBBBBBBBBBBBB0001", barcode_value="X"
        )
        RfidScan.objects.create(
            empresa=self.empresa_a, lector=self.lector_a, epc="bbbbbbbbbbbbbbbbbbbb0001"
        )

        scan = self._api(self.usuario_a).get(self.SCANS_URL).json()["scans"][0]

        self.assertFalse(scan["match_impresion"])
        self.assertNotIn("impresion_folio", scan)

    def _etiqueta_a(self, epc="00001A7F61EC000474479706"):
        impresion = EtiquetaRFIDImpresion.objects.create(
            empresa=self.empresa_a, sucursal=self.sucursal_a, cantidad=1, status="EXITO"
        )
        return EtiquetaRFIDDetalle.objects.create(
            impresion=impresion, epc=epc, barcode_value="X", estado="IMPRESO"
        )

    def test_receive_liga_la_lectura_y_marca_leida(self):
        detalle = self._etiqueta_a()
        self.client.post(
            self.RECEIVE_URL,
            data=json.dumps([{"epcId": detalle.epc.lower()}, {"epcId": "3035C9D350033A40004311F1"}]),
            content_type="application/json",
            HTTP_X_RFID_TOKEN=self.lector_a.token,
        )

        nuestra = RfidScan.objects.get(epc=detalle.epc)
        ajena = RfidScan.objects.get(epc="3035C9D350033A40004311F1")
        self.assertEqual(nuestra.etiqueta_id, detalle.pk)
        self.assertIsNone(ajena.etiqueta_id)
        detalle.refresh_from_db()
        self.assertEqual(detalle.estado, "LEIDO")
        self.assertIsNotNone(detalle.leida_at)

        scans = {s["epc"]: s for s in self._api(self.usuario_a).get(self.SCANS_URL).json()["scans"]}
        self.assertTrue(scans[detalle.epc]["match_impresion"])
        self.assertEqual(scans[detalle.epc]["impresion_folio"], detalle.impresion.folio)
        self.assertFalse(scans["3035C9D350033A40004311F1"]["match_impresion"])

    def test_registro_tardio_liga_lecturas_previas(self):
        RfidScan.objects.create(
            empresa=self.empresa_a, lector=self.lector_a, epc="00001A7F61EC000474479706"
        )
        impresion = EtiquetaRFIDImpresion.objects.create(
            empresa=self.empresa_a, sucursal=self.sucursal_a, cantidad=1
        )
        EtiquetaRFIDDetalle.objects.create(
            impresion=impresion, epc="00001A7F61EC000474479706", barcode_value="X"
        )

        from wms.services.rfid_scan_service import ligar_lecturas_previas

        self.assertEqual(ligar_lecturas_previas(impresion), 1)
        self.assertEqual(
            RfidScan.objects.get().etiqueta.epc, "00001A7F61EC000474479706"
        )
        self.assertEqual(impresion.etiquetas.get().estado, "LEIDO")

    # ── Formato real del IoT Connector de Zebra (FX7500/FX9600) ───────

    def _evento_simple(self, epc, antena=1, rssi=-55):
        return {
            "data": {"eventNum": 1, "format": "epc", "idHex": epc, "antenna": antena, "peakRssi": rssi},
            "timestamp": "2026-11-05T10:00:00.000-0600",
            "type": "SIMPLE",
        }

    def test_receive_basic_auth_con_token_como_password(self):
        """El HTTP POST del IoT Connector solo ofrece Basic: contraseña = token."""
        import base64

        cred = base64.b64encode(f"fx-toneles:{self.lector_a.token}".encode()).decode()
        resp = self._post_receive(HTTP_AUTHORIZATION=f"Basic {cred}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RfidScan.objects.get().lector_id, self.lector_a.pk)

        malo = base64.b64encode(b"fx-toneles:otro").decode()
        self.assertEqual(self._post_receive(HTTP_AUTHORIZATION=f"Basic {malo}").status_code, 401)

    def test_receive_un_solo_evento_simple(self):
        """Un evento suelto ({"data": {...}}) antes se descartaba completo."""
        detalle = self._etiqueta_a()
        resp = self._post_receive(
            self._evento_simple(detalle.epc.lower(), antena=2, rssi=-61),
            HTTP_X_RFID_TOKEN=self.lector_a.token,
        )
        self.assertEqual(resp.status_code, 200)
        scan = RfidScan.objects.get()
        self.assertEqual((scan.epc, scan.antenna, scan.rssi), (detalle.epc, 2, -61.0))
        self.assertEqual(scan.etiqueta_id, detalle.pk)

    def test_receive_lote_en_arreglo_y_en_ndjson(self):
        detalle = self._etiqueta_a()
        ajeno = "3035C9D350033A40004311F1"
        self._post_receive(
            [self._evento_simple(detalle.epc), self._evento_simple(ajeno)],
            HTTP_X_RFID_TOKEN=self.lector_a.token,
        )
        ndjson = "\n".join(json.dumps(self._evento_simple(e)) for e in (detalle.epc, ajeno))
        # Otro lector: con el mismo, la segunda vuelta caería en la deduplicación.
        resp = self.client.post(
            self.RECEIVE_URL, data=ndjson, content_type="application/json",
            HTTP_X_RFID_TOKEN=self.lector_a2.token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RfidScan.objects.filter(etiqueta=detalle).count(), 2)
        self.assertEqual(RfidScan.objects.filter(epc=ajeno, etiqueta__isnull=True).count(), 2)

    def test_receive_ignora_eventos_sin_epc_hex(self):
        """Un heartbeat o evento de gestión no se guarda como lectura."""
        self._post_receive(
            {"type": "heartbeat", "data": {"tagSummary": "sin lecturas", "hostName": "FX9600-TONELES"}},
            HTTP_X_RFID_TOKEN=self.lector_a.token,
        )
        self.assertFalse(RfidScan.objects.exists())

    def test_lector_sin_sucursal_no_valida(self):
        with self.assertRaises(DjangoValidationError):
            LectorRFID(empresa=self.empresa_a, nombre="sin suc").full_clean()

    def test_lector_con_sucursal_de_otra_empresa_no_valida(self):
        lector = LectorRFID(empresa=self.empresa_a, sucursal=self.sucursal_b, nombre="mal")
        with self.assertRaises(DjangoValidationError):
            lector.full_clean()

    def test_heartbeat_sin_tags_marca_conexion(self):
        resp = self.client.post(
            f"/api/receive-scan/?token={self.lector_a.token}", data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        lector = self._api(self.superuser).get(self.STATS_URL).json()["lectores"]
        fila = next(x for x in lector if x["id"] == self.lector_a.pk)
        self.assertEqual(fila["estado"], "CONECTADO_SIN_TAGS")

    # ── Superusuario en su empresa activa (EC-884) ────────────────────

    def _sembrar_huerfanas(self, n):
        """Tags ajenos de ``/api/receive-scan/`` sin token: sin empresa ni lector."""
        RfidScan.objects.bulk_create(
            [RfidScan(empresa=None, lector=None, epc=f"0000{i:08d}") for i in range(n)]
        )

    def _superuser_sin_empresa(self):
        Usuario.objects.filter(pk=self.superuser.pk).update(empresa=None)
        return Usuario.objects.get(pk=self.superuser.pk)

    def test_scans_superuser_solo_ve_su_empresa_activa(self):
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        self._sembrar(self.empresa_a, self.lector_a2, 1, "a2a2")
        self._sembrar(self.empresa_b, self.lector_b, 2, "bbbb")
        self._sembrar_huerfanas(1)

        resp = self._api(self.superuser).get(self.SCANS_URL)

        self.assertEqual(resp.status_code, 200)
        # Toda la empresa activa, sin filtro de sucursal (rama del admin).
        self.assertEqual(
            sorted(s["epc"] for s in resp.json()["scans"]), ["a2a200000000", "aaaa00000000"]
        )

    def test_stats_superuser_solo_cuenta_su_empresa_activa(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 5, "bbbb")
        self._sembrar_huerfanas(1)

        data = self._api(self.superuser).get(self.STATS_URL).json()

        self.assertEqual(data["total_rfidscan_rows"], 2)
        self.assertTrue(all(s["epc"].startswith("aaaa") for s in data["last_5_scans"]))

    def test_clear_superuser_solo_borra_su_empresa_activa(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")

        resp = self._api(self.superuser).post(self.CLEAR_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "success", "deleted": 2, "filtros": self.SIN_FILTROS})
        self.assertFalse(RfidScan.objects.filter(empresa=self.empresa_a).exists())
        self.assertEqual(RfidScan.objects.filter(empresa=self.empresa_b).count(), 3)

    def test_clear_superuser_sigue_a_su_empresa_activa_al_cambiarla(self):
        """Tras ``cambiar-empresa`` (EC-843) a B, el purge borra sólo las de B."""
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")
        resp = self._api(self.superuser).post(
            "/api/v1/nucleo/cambiar-empresa/", {"empresa": self.empresa_b.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # El JWT sólo lleva el id: cada petición vuelve a leer al usuario de la BD.
        user = Usuario.objects.get(pk=self.superuser.pk)
        self.assertEqual(user.empresa_id, self.empresa_b.pk)

        resp = self._api(user).post(self.CLEAR_URL)

        self.assertEqual(resp.json(), {"status": "success", "deleted": 3, "filtros": self.SIN_FILTROS})
        self.assertFalse(RfidScan.objects.filter(empresa=self.empresa_b).exists())
        self.assertEqual(RfidScan.objects.filter(empresa=self.empresa_a).count(), 2)

    def test_superuser_sin_empresa_no_ve_ni_borra_nada(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")
        self._sembrar_huerfanas(1)
        api = self._api(self._superuser_sin_empresa())

        self.assertEqual(api.get(self.SCANS_URL).json()["scans"], [])
        self.assertEqual(api.get(self.STATS_URL).json()["total_rfidscan_rows"], 0)
        resp = api.post(self.CLEAR_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "success", "deleted": 0, "filtros": self.SIN_FILTROS})
        self.assertEqual(RfidScan.objects.count(), 6)

    def _assert_clear_no_borra_huerfanas(self, user):
        """Las huérfanas no las purga nadie por la API (sólo el Django admin)."""
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        self._sembrar_huerfanas(2)

        resp = self._api(user).post(self.CLEAR_URL)

        self.assertEqual(resp.json()["deleted"], 1)
        self.assertEqual(RfidScan.objects.filter(empresa__isnull=True).count(), 2)

    def test_clear_superuser_no_borra_lecturas_sin_empresa(self):
        self._assert_clear_no_borra_huerfanas(self.superuser)

    def test_clear_admin_no_borra_lecturas_sin_empresa(self):
        self._assert_clear_no_borra_huerfanas(self.admin_a)

    def test_scans_no_admin_no_ve_huerfanas_por_la_rama_sin_lector(self):
        """La rama ``lector NULL + etiqueta__impresion__sucursal`` de ``visibles_para``
        sólo trae lecturas de la empresa activa: una huérfana ligada a una etiqueta
        de la sucursal del usuario no se cuela."""
        detalles = {}
        for sucursal, epc in ((self.sucursal_a, "A1A1A1A1A1A1A1A1A1A10001"), (self.sucursal_a2, "A2A2A2A2A2A2A2A2A2A20001")):
            impresion = EtiquetaRFIDImpresion.objects.create(
                empresa=self.empresa_a, sucursal=sucursal, cantidad=1
            )
            detalles[sucursal.pk] = EtiquetaRFIDDetalle.objects.create(
                impresion=impresion, epc=epc, barcode_value="X"
            )
        self._sembrar(self.empresa_a, self.lector_a, 1, "aaaa")
        RfidScan.objects.bulk_create([
            # Sin lector, etiqueta de su sucursal: visible.
            RfidScan(empresa=self.empresa_a, etiqueta=detalles[self.sucursal_a.pk], epc="A1A1A1A1A1A1A1A1A1A10001"),
            # Sin lector, etiqueta de otra sucursal de su empresa: no visible.
            RfidScan(empresa=self.empresa_a, etiqueta=detalles[self.sucursal_a2.pk], epc="A2A2A2A2A2A2A2A2A2A20001"),
            # Huérfana ligada a una etiqueta de su sucursal: no visible.
            RfidScan(empresa=None, etiqueta=detalles[self.sucursal_a.pk], epc="a1a1a1a1a1a1a1a1a1a10001"),
        ])
        self._sembrar_huerfanas(2)

        resp = self._api(self.usuario_a).get(self.SCANS_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            sorted(s["epc"] for s in resp.json()["scans"]),
            ["A1A1A1A1A1A1A1A1A1A10001", "aaaa00000000"],
        )

    def test_clear_es_un_solo_delete(self):
        """Un ``DELETE`` de lecturas y uno de sus ``RfidLecturaUltima`` en cascada
        (#1460), sin importar cuántas filas; ``deleted`` cuenta solo lecturas."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone

        self._sembrar(self.empresa_a, self.lector_a, 3, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 2, "bbbb")
        RfidLecturaUltima.objects.bulk_create([
            RfidLecturaUltima(empresa=self.empresa_a, lector=self.lector_a, epc=s.epc, scan=s, visto=timezone.now())
            for s in RfidScan.objects.filter(empresa=self.empresa_a)
        ])
        api = self._api(self.superuser)

        with CaptureQueriesContext(connection) as ctx:
            resp = api.post(self.CLEAR_URL)

        self.assertEqual(resp.json()["deleted"], 3)
        deletes = [q["sql"] for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("DELETE")]
        self.assertEqual(len(deletes), 2, deletes)
        self.assertIn('"rfid_lecturas_ultimas"', deletes[0])
        self.assertIn('"rfid_scans"', deletes[1])
        self.assertFalse(RfidLecturaUltima.objects.exists())

    def test_clear_solo_borra_la_empresa_propia(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 3, "bbbb")

        resp = self._api(self.admin_a).post(self.CLEAR_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(RfidScan.objects.filter(empresa=self.empresa_a).exists())
        self.assertEqual(RfidScan.objects.filter(empresa=self.empresa_b).count(), 3)

    def test_stats_solo_cuenta_la_empresa_propia(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 5, "bbbb")

        data = self._api(self.admin_a).get(self.STATS_URL).json()

        self.assertEqual(data["total_rfidscan_rows"], 2)
        self.assertTrue(all(s["epc"].startswith("aaaa") for s in data["last_5_scans"]))

    def test_stats_exige_admin(self):
        self.assertEqual(self._api(self.usuario_a).get(self.STATS_URL).status_code, 403)


class PickingGeneraTransferenciaTests(TestCase):
    """El picking genera el traspaso (folio TR) origen → destino y mueve el stock."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal

        from inventarios.models import Almacen, Existencia, MovimientoInventario
        from nucleo.models import Moneda, SerieFolio
        from terceros.models import Cliente
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla
        from catalogo.models import Talla

        cls.Decimal = Decimal
        cls.Existencia = Existencia
        cls.MovimientoInventario = MovimientoInventario
        cls.empresa = Empresa.objects.create(codigo="tr-pick", razon_social="TR SA")
        cls.sucursal = Sucursal.objects.create(
            empresa=cls.empresa, codigo="MTZ", nombre="Matriz"
        )
        cls.usuario = Usuario.objects.create(
            username="tr@example.com",
            email="tr@example.com",
            empresa=cls.empresa,
            sucursal_default=cls.sucursal,
            is_admin_empresa=True,
        )
        for tipo, prefijo in (("Picking", "PK"), ("Transferencia", "TR")):
            SerieFolio.objects.create(
                empresa=cls.empresa,
                sucursal=cls.sucursal,
                tipo_documento=tipo,
                serie="A",
                prefijo=prefijo,
            )
        cls.origen = Almacen.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            codigo="PT",
            nombre="Terminado",
            permite_salida=True,
            permite_entrada=True,
        )
        cls.destino = Almacen.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            codigo="APA",
            nombre="Apartados",
            permite_entrada=True,
            permite_salida=True,
        )
        cls.producto = Producto.objects.create(
            tipo=_tipo_producto(),
            empresa=cls.empresa, nombre="Playera", codigo="PL01"
        )
        Existencia.objects.create(
            producto=cls.producto,
            almacen=cls.origen,
            stock=10,
            cantidad=Decimal("10"),
        )
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa,
            sucursal=cls.sucursal,
            cliente=cliente,
            moneda=moneda,
            folio="P-1",
            # AUTORIZADA: el listado del onboarding solo trae ``estatus`` 3/4.
            estatus=3,
            programacion_conf={"programaciones": [{"destino": "APARTADO", "cantidad": 1}]},
            persona_pagos="Pagos",
            correo_facturas="tr@example.com",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=cls.pedido, producto=cls.producto)
        cls.talla_row = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=Talla.objects.create(nombre="M"), cantidad=4
        )

    def _data(self, cantidad):
        return {
            "pedido": self.pedido,
            "almacen": self.origen,
            "almacen_destino": self.destino,
            "operador": self.usuario,
            "picking_detalle": [
                {"pedido_detalle_talla": self.talla_row.pk, "cantidad_asignada": cantidad}
            ],
        }

    def test_alta_expone_almacen_actual(self):
        """EC-607: el alta (normal y por onboarding) responde dónde quedan las prendas."""
        api = APIClient()
        api.force_authenticate(self.usuario)
        cuerpo = {
            "pedido": self.pedido.pk,
            "operador": self.usuario.pk,
            "almacen": self.origen.pk,
            "almacen_destino": self.destino.pk,
            "picking_detalle": [{"pedido_detalle_talla": self.talla_row.pk, "cantidad_asignada": "1"}],
        }
        for url in ("/api/v1/wms/pickings/", "/api/v1/wms/pickings/onboarding/"):
            resp = api.post(url, cuerpo, format="json")
            self.assertEqual(resp.status_code, 201, resp.content)
            self.assertIsNotNone(resp.json()["transferencia"])
            self.assertEqual(
                (resp.json()["almacen_actual"], resp.json()["almacen_actual_nombre"]),
                (self.destino.pk, "Apartados"),
            )

    def test_onboarding_solo_pedidos_programados_con_almacen_sugerido(self):
        from ventas.models import Pedido

        api = APIClient()
        api.force_authenticate(self.usuario)
        sin_programar = Pedido.objects.get(pk=self.pedido.pk)
        sin_programar.pk, sin_programar.folio, sin_programar.programacion_conf = None, "P-2", None
        sin_programar.save()
        data = api.get("/api/v1/wms/pickings/onboarding/").json()
        self.assertEqual([p["id"] for p in data["pedidos"]], [self.pedido.pk])
        data = api.get(f"/api/v1/wms/pickings/onboarding/?pedido={self.pedido.pk}").json()
        (prog,) = data["pedido"]["programaciones"]
        self.assertEqual((prog["destino"], prog["almacen_sugerido"]["id"]), ("APARTADO", self.destino.pk))
        cuerpo = {
            "pedido": sin_programar.pk, "operador": self.usuario.pk, "almacen": self.origen.pk,
            "almacen_destino": self.destino.pk,
            "picking_detalle": [{"pedido_detalle_talla": self.talla_row.pk, "cantidad_asignada": "1"}],
        }
        resp = api.post("/api/v1/wms/pickings/onboarding/", cuerpo, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_alta_responde_el_detalle_precargado(self):
        """EC-882: el alta de picking y de packing responde lo mismo que su retrieve, y
        la respuesta del packing no suma consultas por renglón (antes cada FK de cada
        renglón era una consulta)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from catalogo.models import Talla
        from nucleo.models import SerieFolio
        from ventas.models import PedidoDetalleTalla

        SerieFolio.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, tipo_documento="Packing", serie="A", prefijo="PAK"
        )
        talla_l = PedidoDetalleTalla.objects.create(
            pedido_detalle=self.talla_row.pedido_detalle, talla=Talla.objects.create(nombre="L"), cantidad=4
        )
        api = APIClient()
        api.force_authenticate(self.usuario)
        consultas_packing = []
        for tallas in ([self.talla_row], [self.talla_row, talla_l]):
            resp = api.post(
                "/api/v1/wms/pickings/",
                {
                    "pedido": self.pedido.pk, "operador": self.usuario.pk, "almacen": self.origen.pk,
                    "almacen_destino": self.destino.pk,
                    "picking_detalle": [{"pedido_detalle_talla": t.pk, "cantidad_asignada": "1"} for t in tallas],
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 201, resp.content)
            picking = resp.json()
            self.assertEqual(picking.pop("ordenes_trabajo_generadas"), [])
            self.assertEqual(picking, api.get(f"/api/v1/wms/pickings/{picking['id']}/").json())

            with CaptureQueriesContext(connection) as ctx:
                resp = api.post(
                    "/api/v1/wms/packings/",
                    {
                        "picking": picking["id"],
                        "packing_detalle": [
                            {"picking_detalle": linea["id"], "cantidad_empacada": "1"}
                            for linea in picking["picking_detalle"]
                        ],
                    },
                    format="json",
                )
            self.assertEqual(resp.status_code, 201, resp.content)
            consultas_packing.append(len(ctx.captured_queries))
            packing = resp.json()
            self.assertEqual(packing, api.get(f"/api/v1/wms/packings/{packing['id']}/").json())
        self.assertEqual(consultas_packing[1], consultas_packing[0])

    def _retener_en_bd(self):
        from ventas.models import Pedido

        Pedido.objects.filter(pk=self.pedido.pk).update(
            retenido=True, motivo_retencion="Crédito", retenido_por=self.usuario,
        )
        return Pedido.objects.get(pk=self.pedido.pk)

    def test_alta_rechaza_pedido_retenido(self):
        """EC-953: el alta de picking (normal y por onboarding) rechaza un pedido retenido."""
        from ventas.services.retencion_service import mensaje_retenido

        retenido = self._retener_en_bd()
        api = APIClient()
        api.force_authenticate(self.usuario)
        cuerpo = {
            "pedido": self.pedido.pk, "operador": self.usuario.pk, "almacen": self.origen.pk,
            "almacen_destino": self.destino.pk,
            "picking_detalle": [{"pedido_detalle_talla": self.talla_row.pk, "cantidad_asignada": "1"}],
        }
        for url in ("/api/v1/wms/pickings/", "/api/v1/wms/pickings/onboarding/"):
            resp = api.post(url, cuerpo, format="json")
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(resp.json(), {"pedido": mensaje_retenido(retenido)})
        from wms.models import Picking
        self.assertFalse(Picking.objects.filter(pedido=self.pedido).exists())

    def test_retencion_se_revisa_con_la_fila_del_pedido_bloqueada(self):
        """EC-953: como packing y despacho, la retención se lee de la fila bloqueada, no
        del pedido ya cargado: un ``retener/`` entre la lectura y el alta (aquí, el
        pedido en memoria sigue libre y la BD ya lo tiene retenido) no deja pasar el picking."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.exceptions import ValidationError

        from wms.services.picking_service import PickingService

        self._retener_en_bd()
        self.assertFalse(self.pedido.retenido)  # copia en memoria, leída antes del ``retener/``
        with CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(ValidationError) as err:
                PickingService.handle_store(self._data(self.Decimal("1")), self.usuario)

        self.assertIn("retenido", str(err.exception.detail["pedido"]))
        self.assertTrue(any(
            "FOR UPDATE" in q["sql"].upper() and 'FROM "pedidos"' in q["sql"] for q in ctx.captured_queries
        ))
        from wms.models import Picking
        self.assertFalse(Picking.objects.filter(pedido=self.pedido).exists())

    def test_cancelacion_se_revisa_con_la_fila_del_pedido_bloqueada(self):
        """EC-953: un ``cancelar/`` entre la lectura del pedido y el alta tampoco se cuela:
        el pedido en memoria sigue autorizado, la fila bloqueada ya está cancelada."""
        from rest_framework.exceptions import ValidationError

        from ventas.models import Pedido
        from wms.models import Picking
        from wms.services.picking_service import PickingService

        Pedido.objects.filter(pk=self.pedido.pk).update(estatus=Pedido.ESTATUS_CANCELADO, activo=False)
        self.assertEqual(self.pedido.estatus, 3)
        with self.assertRaises(ValidationError) as err:
            PickingService.handle_store(self._data(self.Decimal("1")), self.usuario)
        self.assertEqual(err.exception.detail, {"pedido": "El pedido está CANCELADO; no se puede surtir."})

        Pedido.objects.filter(pk=self.pedido.pk).update(estatus=3, activo=False)
        with self.assertRaises(ValidationError) as err:
            PickingService.handle_store(self._data(self.Decimal("1")), self.usuario)
        self.assertEqual(
            err.exception.detail, {"pedido": "El pedido no está autorizado ni en proceso; no se puede surtir."},
        )
        self.assertFalse(Picking.objects.filter(pedido=self.pedido).exists())

    def test_pedido_que_ya_no_existe_responde_400(self):
        """EC-953: si el pedido desaparece antes del candado, 400 de pedido no encontrado
        (antes pasaba la revisión con ``None`` y reventaba con ``IntegrityError``)."""
        from unittest import mock

        from rest_framework.exceptions import ValidationError

        from ventas.models import Pedido
        from wms.services.picking_service import PickingService

        vacio = Pedido.objects.none()
        with mock.patch.object(Pedido.objects, "select_for_update", return_value=vacio):
            with self.assertRaises(ValidationError) as err:
                PickingService.handle_store(self._data(self.Decimal("1")), self.usuario)
        self.assertEqual(err.exception.detail, {"pedido": "Pedido no encontrado o sin acceso."})

    def test_picking_crea_transferencia_con_folio_tr_y_mueve_stock(self):
        from wms.services.picking_service import PickingService

        picking, _ = PickingService.handle_store(self._data(self.Decimal("3")), self.usuario)

        self.assertIsNotNone(picking.transferencia_id)
        self.assertEqual(picking.transferencia.picking_id, picking.pk)  # EC-916
        self.assertTrue(picking.transferencia.folio.startswith("TR"))
        self.assertEqual(picking.transferencia.almacen_origen, self.origen)
        self.assertEqual(picking.transferencia.almacen_destino, self.destino)
        self.assertEqual(
            self.Existencia.objects.get(almacen=self.origen).cantidad, self.Decimal("7")
        )
        self.assertEqual(
            self.Existencia.objects.get(almacen=self.destino).cantidad, self.Decimal("3")
        )
        self.assertTrue(
            self.MovimientoInventario.objects.filter(
                transferencia=picking.transferencia
            ).exists()
        )

class OnboardingAlmacenSugeridoTests(TestCase):
    """EC-645: ``almacen_sugerido`` en ambas respuestas del GET onboarding y
    sugerencias de origen/destino solo entre almacenes que el POST aceptaría."""

    URL = "/api/v1/wms/pickings/onboarding/"

    @classmethod
    def setUpTestData(cls):
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente

        cls.empresa = Empresa.objects.create(codigo="ec645", razon_social="EC645 SA")
        cls.matriz = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.norte = Sucursal.objects.create(empresa=cls.empresa, codigo="NTE", nombre="Norte")
        # Admin de empresa: el listado le trae pedidos de las dos sucursales.
        cls.usuario = Usuario.objects.create(
            username="ec645@example.com", email="ec645@example.com", empresa=cls.empresa,
            sucursal_default=cls.matriz, is_admin_empresa=True,
        )

        def almacen(sucursal, codigo, nombre, empresa=None, **flags):
            return Almacen.objects.create(
                empresa=empresa or cls.empresa, sucursal=sucursal, codigo=codigo, nombre=nombre, **flags
            )

        # Matriz. El orden de alta fija los pk: lo inválido va primero, que es lo
        # que una regla de "menor pk" sin filtro elegiría.
        cls.origen_inactivo = almacen(cls.matriz, "PT-OFF", "PT viejo", estatus="INACTIVO", permite_salida=True)
        cls.sin_salida = almacen(cls.matriz, "PT-NS", "Sin salida")
        cls.apartados_inactivo = almacen(cls.matriz, "APA-OFF", "APARTADOS", estatus="INACTIVO", permite_entrada=True)
        cls.apartados = almacen(cls.matriz, "APA", "Apartados", permite_entrada=True)
        cls.apartados_dup = almacen(cls.matriz, "APA2", "APARTADOS", permite_entrada=True)
        cls.origen = almacen(cls.matriz, "PT", "Terminado", permite_salida=True)
        almacen(cls.matriz, "BOR", "BORDADOS", estatus="INACTIVO", permite_entrada=True)
        # Norte: sin APARTADOS propio; un BORDADOS sin entrada y otro válido.
        almacen(cls.norte, "BOR-NE", "Bordados")
        cls.bordados_norte = almacen(cls.norte, "BOR-OK", "BORDADOS", permite_entrada=True)
        # Otra empresa con un APARTADOS válido colgado de la sucursal Norte: no
        # debe sugerirse nunca (el candidato se acota a la empresa del pedido).
        otra = Empresa.objects.create(codigo="ec645-b", razon_social="Otra SA")
        almacen(cls.norte, "APA-X", "APARTADOS", empresa=otra, permite_entrada=True)

        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        cls.pedido_matriz = cls._pedido(cls.matriz, "M-1", ["APARTADO", "BORDADO", "EMBARQUE"])
        cls.pedido_norte = cls._pedido(cls.norte, "N-1", ["APARTADO", "BORDADO"])

    @classmethod
    def _pedido(cls, sucursal, folio, destinos, pendiente=True):
        from catalogo.models import Producto, Talla
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

        pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=sucursal, cliente=cls.cliente, moneda=cls.moneda, folio=folio,
            estatus=3, programacion_conf={"programaciones": [{"destino": d, "cantidad": 1} for d in destinos]},
            persona_pagos="Pagos", correo_facturas="ec645@example.com", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        if pendiente:
            # El listado solo ofrece pedidos con algo surtible (#823).
            detalle = PedidoDetalle.objects.create(
                pedido=pedido, producto=Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre=f"Prod {folio}"),
            )
            PedidoDetalleTalla.objects.create(
                pedido_detalle=detalle, talla=Talla.objects.create(nombre=f"T-{folio}"), cantidad=1,
            )
        return pedido

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.usuario)

    def _get(self, **params):
        resp = self.api.get(self.URL, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    @staticmethod
    def _sugeridos(programaciones):
        return [(p["destino"], (p["almacen_sugerido"] or {}).get("id")) for p in programaciones]

    def test_listado_trae_almacen_sugerido_por_sucursal(self):
        pedidos = {p["id"]: p for p in self._get()["pedidos"]}
        # Matriz: APARTADOS activo de menor pk (no el INACTIVO, no el duplicado);
        # BORDADOS inactivo -> null; EMBARQUE también sale de APARTADOS (bf979686).
        self.assertEqual(
            self._sugeridos(pedidos[self.pedido_matriz.pk]["programaciones"]),
            [("APARTADO", self.apartados.pk), ("BORDADO", None), ("EMBARQUE", self.apartados.pk)],
        )
        # Norte: sin APARTADOS propio (el de otra empresa no cuenta) -> null; el
        # BORDADOS sin ``permite_entrada`` se salta aunque tenga menor pk.
        self.assertEqual(
            self._sugeridos(pedidos[self.pedido_norte.pk]["programaciones"]),
            [("APARTADO", None), ("BORDADO", self.bordados_norte.pk)],
        )
        self.assertEqual(
            pedidos[self.pedido_matriz.pk]["programaciones"][0]["almacen_sugerido"],
            {"id": self.apartados.pk, "nombre": "Apartados"},
        )

    def test_listado_y_detalle_coinciden(self):
        listado = {p["id"]: p["programaciones"] for p in self._get()["pedidos"]}
        for pedido in (self.pedido_matriz, self.pedido_norte):
            detalle = self._get(pedido=pedido.pk)["pedido"]["programaciones"]
            self.assertEqual(detalle, listado[pedido.pk])

    def test_listado_no_crece_en_consultas_con_mas_pedidos(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as antes:
            self._get()
        for i in range(4):
            self._pedido(self.matriz if i % 2 else self.norte, f"X-{i}", ["APARTADO", "BORDADO"])
        with CaptureQueriesContext(connection) as despues:
            data = self._get()
        self.assertEqual(len(data["pedidos"]), 6)
        self.assertEqual(len(despues), len(antes))

    def test_detalle_reusa_los_almacenes_del_listado(self):
        import wms.services.picking_service as picking_service

        espia = patch.object(
            picking_service, "almacenes_por_destino", wraps=picking_service.almacenes_por_destino
        )
        with espia as llamada:
            data = self._get(pedido=self.pedido_matriz.pk)
        llamada.assert_not_called()  # su sucursal ya venía resuelta del listado
        self.assertEqual(data["pedido"]["programaciones"][0]["almacen_sugerido"]["id"], self.apartados.pk)

        # Fuera de los 50 del listado: se resuelve aparte, con el mismo resultado.
        for i in range(50):
            self._pedido(self.norte, f"R-{i}", ["APARTADO"])
        with espia as llamada:
            data = self._get(pedido=self.pedido_matriz.pk)
        llamada.assert_called_once()
        self.assertEqual(data["pedido"]["programaciones"][0]["almacen_sugerido"]["id"], self.apartados.pk)
        self.assertEqual(data["almacen_destino"]["id"], self.apartados.pk)

    def test_sugerencias_de_origen_y_destino_filtran_activo_y_flags(self):
        data = self._get(pedido=self.pedido_matriz.pk)
        # Destino: APARTADOS activo con entrada de menor pk (salta el INACTIVO).
        self.assertEqual(data["almacen_destino"]["id"], self.apartados.pk)
        # Origen: activo con ``permite_salida`` (salta el INACTIVO y el sin salida).
        self.assertEqual(data["almacen_origen"]["id"], self.origen.pk)

    def _sucursal_con(self, codigo, *almacenes):
        """Sucursal nueva con sus almacenes ``(codigo, nombre, flags)`` y un pedido programado."""
        from inventarios.models import Almacen

        sucursal = Sucursal.objects.create(empresa=self.empresa, codigo=codigo, nombre=codigo)
        creados = [
            Almacen.objects.create(empresa=self.empresa, sucursal=sucursal, codigo=c, nombre=n, **flags)
            for c, n, flags in almacenes
        ]
        return self._pedido(sucursal, f"{codigo}-1", ["APARTADO"]), creados

    def test_origen_nunca_es_el_destino(self):
        # Solo hay un APARTADOS válido como destino que además permite salida.
        pedido, (apartados,) = self._sucursal_con(
            "SUR", ("APA", "APARTADOS", {"permite_entrada": True, "permite_salida": True})
        )
        data = self._get(pedido=pedido.pk)
        self.assertEqual(data["almacen_destino"]["id"], apartados.pk)
        self.assertIsNone(data["almacen_origen"])

    def test_origen_nunca_es_apartados_aunque_no_sea_destino(self):
        # APARTADOS sin entrada (no es destino) pero con salida y el menor pk.
        pedido, (_, terminado) = self._sucursal_con(
            "OTE",
            ("APA", "APARTADOS", {"permite_salida": True}),
            ("PT", "Terminado", {"permite_salida": True}),
        )
        data = self._get(pedido=pedido.pk)
        self.assertIsNone(data["almacen_destino"])
        self.assertEqual(data["almacen_origen"]["id"], terminado.pk)

    def test_origen_por_query_param_conserva_la_regla_del_destino(self):
        data = self._get(pedido=self.pedido_matriz.pk, almacen_origen=self.origen.pk)
        self.assertEqual(data["almacen_origen"]["id"], self.origen.pk)
        # El destino sale igual que sin query param: APARTADOS activo con entrada.
        self.assertEqual(data["almacen_destino"]["id"], self.apartados.pk)

        data = self._get(pedido=self.pedido_norte.pk, almacen_origen=self.bordados_norte.pk)
        self.assertEqual(data["almacen_origen"]["id"], self.bordados_norte.pk)
        # Su única programación con almacén es el origen pedido, y Norte no
        # tiene APARTADOS válido: sin destino.
        self.assertIsNone(data["almacen_destino"])

    def test_usuario_de_una_sucursal_solo_ve_sus_pedidos_y_sugerencias(self):
        operador = Usuario.objects.create(
            username="norte645@example.com", email="norte645@example.com",
            empresa=self.empresa, sucursal_default=self.norte,
        )
        self.api.force_authenticate(operador)
        (pedido,) = self._get()["pedidos"]
        self.assertEqual(pedido["id"], self.pedido_norte.pk)
        self.assertEqual(
            self._sugeridos(pedido["programaciones"]),
            [("APARTADO", None), ("BORDADO", self.bordados_norte.pk)],
        )
        resp = self.api.get(self.URL, {"pedido": self.pedido_matriz.pk})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_destino_sale_de_las_programaciones_si_apuntan_a_uno_solo(self):
        # #816: Norte programa APARTADO (sin almacén) y BORDADO -> BORDADOS.
        data = self._get(pedido=self.pedido_norte.pk)
        self.assertEqual(data["almacen_destino"]["id"], self.bordados_norte.pk)

    def test_sin_programacion_con_almacen_ni_apartados_no_sugiere_destino(self):
        pedido, _ = self._sucursal_con("PTE", ("PT", "Terminado", {"permite_salida": True}))
        self.assertIsNone(self._get(pedido=pedido.pk)["almacen_destino"])

    def test_ids_invalidos_son_400(self):
        # #817: antes ``?pedido=abc`` daba el listado y los almacenes se sustituían.
        for params, campo in (
            ({"pedido": "abc"}, "pedido"),
            ({"pedido": "0"}, "pedido"),
            ({"pedido": self.pedido_matriz.pk, "almacen_destino": "abc"}, "almacen_destino"),
            ({"pedido": self.pedido_matriz.pk, "almacen_destino": 999999}, "almacen_destino"),
            ({"pedido": self.pedido_matriz.pk, "almacen_origen": 999999}, "almacen_origen"),
            ({"almacen_origen": "abc"}, "almacen_origen"),
        ):
            resp = self.api.get(self.URL, params)
            self.assertEqual(resp.status_code, 400, (params, resp.content))
            self.assertIn(campo, resp.json())

    def test_listado_omite_pedidos_sin_pendiente(self):
        from ventas.models import PedidoDetalle

        sin_tallas = self._pedido(self.matriz, "S-1", ["APARTADO"], pendiente=False)
        solo_muestra = self._pedido(self.matriz, "S-2", ["APARTADO"], pendiente=False)
        PedidoDetalle.objects.create(pedido=solo_muestra, producto_nombre_externo="Muestra")
        ids = {p["id"] for p in self._get()["pedidos"]}
        self.assertNotIn(sin_tallas.pk, ids)
        self.assertNotIn(solo_muestra.pk, ids)
        self.assertIn(self.pedido_matriz.pk, ids)

    def test_alta_rechaza_pedido_no_autorizado_o_inactivo(self):
        # #822: el alta repite las reglas del listado.
        from types import SimpleNamespace

        from rest_framework.exceptions import ValidationError

        from wms.services.picking_pipeline.context import validar_contexto_picking

        for estatus, activo in ((1, True), (2, True), (3, False)):
            pedido = SimpleNamespace(
                empresa_id=self.empresa.pk, estatus=estatus, activo=activo, ESTATUS_CANCELADO=5,
                retenido=False, motivo_retencion="",  # #1359
                programacion_conf={"programaciones": [{"destino": "APARTADO"}]},
            )
            with self.assertRaises(ValidationError) as ctx:
                validar_contexto_picking(pedido, self.origen, self.apartados, None, self.usuario)
            self.assertIn("pedido", ctx.exception.detail)

    def test_listado_avisa_si_se_recorto(self):
        # #818: 2 pedidos; con límite 1 se recorta y lo dice.
        data = self._get(pedidos_limit=1)
        self.assertEqual((len(data["pedidos"]), data["pedidos_truncado"]), (1, True))
        data = self._get()
        self.assertEqual((len(data["pedidos"]), data["pedidos_truncado"]), (2, False))
        self.assertEqual(self.api.get(self.URL, {"pedidos_limit": "x"}).status_code, 400)

    def test_programaciones_null_no_revienta(self):
        from types import SimpleNamespace

        from wms.services.picking_pipeline.catalogs import programaciones_de

        self.assertEqual(programaciones_de(SimpleNamespace(programacion_conf={"programaciones": None}), {}), [])


class PickingTrasladoTests(TestCase):
    """``POST /pickings/{id}/trasladar/``: mover un picking a otro almacén."""

    @classmethod
    def setUpTestData(cls):
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente
        from ventas.models import Pedido
        from wms.models import Picking

        cls.empresa = Empresa.objects.create(codigo="trs", razon_social="Traslado SA")
        cls.otra_empresa = Empresa.objects.create(codigo="trs2", razon_social="Otra SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.admin = Usuario.objects.create(
            username="trs-admin", email="trs-admin@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        kw = dict(empresa=cls.empresa, sucursal=cls.sucursal, permite_salida=True, permite_entrada=True)
        cls.bordados = Almacen.objects.create(codigo="BOR", nombre="Bordados", **kw)
        cls.apartados = Almacen.objects.create(codigo="APA", nombre="Apartados", **kw)
        cls.ajeno = Almacen.objects.create(
            codigo="X", nombre="Ajeno", empresa=cls.otra_empresa, permite_salida=True
        )
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=moneda
        )
        cls.picking = Picking.objects.create(
            folio="PK-1", empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido,
            operador=cls.admin, almacen=cls.bordados, almacen_destino=cls.apartados,
            usuario=cls.admin,
        )

    def _post(self, almacen, user=None):
        client = APIClient()
        client.force_authenticate(user or self.admin)
        return client.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/",
            {"almacen": almacen.pk, "observaciones": "Bordado terminado"},
            format="json",
        )

    def test_traslada_y_limpia_destino_si_coincide(self):
        resp = self._post(self.apartados)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.picking.refresh_from_db()
        self.assertEqual(self.picking.almacen_id, self.apartados.pk)
        self.assertIsNone(self.picking.almacen_destino_id)
        self.assertIn("Bordado terminado", self.picking.observaciones)

    def test_mismo_almacen_rechazado(self):
        self.assertEqual(self._post(self.bordados).status_code, 400)

    def test_almacen_de_otra_empresa_rechazado(self):
        self.assertEqual(self._post(self.ajeno).status_code, 400)

    def test_almacen_sin_empresa_o_sin_sucursal_rechazado(self):
        from inventarios.models import Almacen
        sin_sucursal = Almacen.objects.create(
            codigo="SS", nombre="Sin sucursal", empresa=self.empresa, permite_salida=True
        )
        self.assertEqual(self._post(sin_sucursal).status_code, 400)

    def test_almacen_inactivo_rechazado(self):
        from inventarios.models import Almacen
        Almacen.objects.filter(pk=self.apartados.pk).update(estatus="INACTIVO")
        self.assertEqual(self._post(self.apartados).status_code, 400)

    def test_almacen_ajeno_no_se_distingue_de_uno_inexistente(self):
        """#510: el mensaje no revela que el ID existe en otra empresa."""
        ajeno = self._post(self.ajeno)
        client = APIClient()
        client.force_authenticate(self.admin)
        inexistente = client.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": 999999}, format="json"
        )
        self.assertEqual(ajeno.status_code, 400)
        self.assertEqual(inexistente.status_code, 400)
        self.assertEqual(list(ajeno.json()), list(inexistente.json()))
        self.assertNotIn("empresa", str(ajeno.json()))

    def test_picking_con_lineas_surtidas_se_traslada(self):
        from ventas.models import PedidoDetalle
        from wms.models import PickingDetalle
        PickingDetalle.objects.create(
            picking=self.picking,
            pedido_detalle=PedidoDetalle.objects.create(
                pedido=self.picking.pedido, producto_nombre_externo="Muestra"
            ),
            cantidad_solicitada=2, cantidad_asignada=2, cantidad_surtida=2,
        )
        # Trasladar solo mueve las prendas: lo surtido no lo impide ni cambia.
        self.assertEqual(self._post(self.apartados).status_code, 200)
        self.picking.refresh_from_db()
        self.assertEqual(self.picking.almacen_id, self.apartados.pk)

    def test_picking_cancelado_no_se_traslada(self):
        from wms.models import Picking
        Picking.objects.filter(pk=self.picking.pk).update(estado=Picking.Estado.CANCELADO)
        self.assertEqual(self._post(self.apartados).status_code, 400)

    def test_sin_permiso_c_wms_picking_responde_403(self):  # #695: mueve inventario
        operador = Usuario.objects.create(
            username="trs-op", email="trs-op@example.com", empresa=self.empresa, sucursal_default=self.sucursal
        )
        self.assertEqual(self._post(self.apartados, operador).status_code, 403)
        self.picking.refresh_from_db()
        self.assertEqual(self.picking.almacen_id, self.bordados.pk)

    def test_otra_empresa_recibe_404(self):
        intruso = Usuario.objects.create(
            username="trs-x", email="trs-x@example.com", empresa=self.otra_empresa
        )
        self.assertEqual(self._post(self.apartados, intruso).status_code, 404)

    def test_almacen_actual_sin_transferencia_es_almacen(self):
        """EC-607: con ``almacen_destino`` pero sin TR las prendas siguen en ``almacen``."""
        client = APIClient()
        client.force_authenticate(self.admin)
        resp = client.get(f"/api/v1/wms/pickings/{self.picking.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            (resp.json()["almacen_actual"], resp.json()["almacen_actual_nombre"]),
            (self.bordados.pk, "Bordados"),
        )

    def test_almacen_actual_no_agrega_consultas_al_listado(self):
        """EC-607: ``GET /pickings/`` resuelve ``almacen_actual`` sin consultas por picking.

        Los pickings van sin TR para aislar los campos nuevos de la TR.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from wms.models import Picking

        client = APIClient()
        client.force_authenticate(self.admin)

        def listar():
            with CaptureQueriesContext(connection) as consultas:
                resp = client.get("/api/v1/wms/pickings/")
            self.assertEqual(resp.status_code, 200, resp.content)
            datos = resp.json()
            filas = datos.get("results", datos) if isinstance(datos, dict) else datos
            return len(consultas), {f["folio"]: (f["almacen_actual"], f["almacen_actual_nombre"]) for f in filas}

        pocas, actuales = listar()
        self.assertEqual(actuales, {"PK-1": (self.bordados.pk, "Bordados")})
        for n in range(2, 5):
            Picking.objects.create(
                folio=f"PK-{n}", empresa=self.empresa, sucursal=self.sucursal, pedido=self.picking.pedido,
                operador=self.admin, almacen=self.apartados, usuario=self.admin,
            )
        muchas, actuales = listar()
        self.assertEqual(muchas, pocas)
        self.assertEqual(actuales["PK-4"], (self.apartados.pk, "Apartados"))

class PackingDespachoEC574Tests(TestCase):
    """Crear el picking ya surte lo asignado; empacar solo registra lo empacado
    con lo surtido como tope. Lo empacado siempre se despacha; sin embarque
    parcial el onboarding solo avisa lo que falta por empacar (WM-02).

    Todo por la API sobre un picking creado con ``PickingService``: ninguna
    prueba fija ``cantidad_surtida`` a mano.
    """

    # Patrones con los que el front trata un 400 como "dato desactualizado"
    # (useCreatePacking.ts / useCreateShipping.ts): los rechazos nuevos no
    # deben calzar con ellos.
    STALE_PACKING_RE = re.compile(r"ya no tiene cantidad pendiente|excede lo pendiente", re.I)
    STALE_DESPACHO_RE = re.compile(r"ya fue despachad", re.I)

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        from inventarios.models import Almacen, Existencia
        from nucleo.models import Moneda, SerieFolio
        from terceros.models import Cliente
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

        cls.empresa = Empresa.objects.create(codigo="ec574", razon_social="EC574 SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.usuario = Usuario.objects.create(
            username="ec574@example.com", email="ec574@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        for tipo, prefijo in (("Picking", "PK"), ("Transferencia", "TR"), ("Packing", "PA")):
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento=tipo, serie="A", prefijo=prefijo,
            )
        kw = dict(empresa=cls.empresa, sucursal=cls.sucursal, permite_salida=True, permite_entrada=True)
        cls.origen = Almacen.objects.create(codigo="PT", nombre="Terminado", **kw)
        cls.destino = Almacen.objects.create(codigo="APA", nombre="Apartados", **kw)
        cls.otro = Almacen.objects.create(codigo="BOR", nombre="Bordados", **kw)
        producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera", codigo="PL574")
        Existencia.objects.create(producto=producto, almacen=cls.origen, stock=20, cantidad=Decimal("20"))
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            cliente=Cliente.objects.create(empresa=cls.empresa, nombre="Cliente"),
            moneda=Moneda.objects.create(codigo_iso="MXN", nombre="Peso"), folio="P-574", estatus=3,
            persona_pagos="Pagos", correo_facturas="ec574@example.com", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            programacion_conf={"programaciones": [{"destino": "APARTADO", "cantidad": 1}]},
        )
        detalle = PedidoDetalle.objects.create(pedido=cls.pedido, producto=producto)
        cls.talla_m = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=Talla.objects.create(nombre="M"), cantidad=4
        )
        cls.talla_g = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=Talla.objects.create(nombre="G"), cantidad=2
        )
        # Una muestra (sin SKU) de 3 piezas: no cuenta en el total del pedido (6).
        cls.detalle_muestra = PedidoDetalle.objects.create(pedido=cls.pedido, producto_nombre_externo="Muestra")
        PedidoDetalleTalla.objects.create(
            pedido_detalle=cls.detalle_muestra, talla=Talla.objects.create(nombre="CH"), cantidad=3
        )

    def setUp(self):
        from wms.services.picking_service import PickingService

        self.api = APIClient()
        self.api.force_authenticate(self.usuario)
        self.picking, _ = PickingService.handle_store(
            {
                "pedido": self.pedido, "almacen": self.origen, "almacen_destino": self.destino,
                "operador": self.usuario,
                "picking_detalle": [
                    {"pedido_detalle_talla": self.talla_m.pk, "cantidad_asignada": Decimal("4")},
                    {"pedido_detalle_talla": self.talla_g.pk, "cantidad_asignada": Decimal("2")},
                ],
            },
            self.usuario,
        )
        lineas = {d.pedido_detalle_talla_id: d for d in self.picking.picking_detalle.all()}
        self.linea_m, self.linea_g = lineas[self.talla_m.pk], lineas[self.talla_g.pk]

    # --- helpers -----------------------------------------------------------
    def _empacar(self, *filas):
        return self.api.post(
            "/api/v1/wms/packings/",
            {
                "picking": self.picking.pk,
                "packing_detalle": [
                    {"picking_detalle": linea.pk, "cantidad_empacada": str(cantidad)} for linea, cantidad in filas
                ],
            },
            format="json",
        )

    def _empacar_ok(self, *filas):
        resp = self._empacar(*filas)
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def _despachar(self, packing):
        lineas = [{"packing_detalle": d["id"]} for d in packing["packing_detalle"]]
        # ``tipo_envio`` es obligatorio (paquetería pide paquetería y guía).
        return self.api.post(
            "/api/v1/wms/despachos/",
            {"packing": packing["id"], "despacho_detalle": lineas,
             "tipo_envio": "PAQUETERIA", "paqueteria": "ESTAFETA", "guia": "G-574"},
            format="json",
        )

    def _mensaje(self, resp, campo):
        cuerpo = resp.json()[campo]
        return cuerpo if isinstance(cuerpo, str) else cuerpo[0]

    # --- WM-01 -------------------------------------------------------------
    def test_picking_nuevo_nace_surtido(self):
        from wms.models import PickingDetalle

        for linea, asignada in ((self.linea_m, 4), (self.linea_g, 2)):
            self.assertEqual(linea.cantidad_surtida, Decimal(asignada))
            self.assertEqual(linea.estado, PickingDetalle.EstadoLinea.SURTIDA)
            self.assertIsNotNone(linea.fecha_surtido)

    def test_empacar_no_cambia_el_surtido(self):
        from auditoria.models import AuditoriaEvento
        from wms.models import Picking, PickingDetalle

        estado = self.picking.estado
        self._empacar_ok((self.linea_m, 3))
        self._empacar_ok((self.linea_m, 1), (self.linea_g, 2))

        self.picking.refresh_from_db()
        for linea, surtida in ((self.linea_m, 4), (self.linea_g, 2)):
            linea.refresh_from_db()
            self.assertEqual(linea.cantidad_surtida, Decimal(surtida))
            self.assertEqual(linea.estado, PickingDetalle.EstadoLinea.SURTIDA)
        self.assertEqual(self.picking.estado, estado)
        self.assertFalse(AuditoriaEvento.objects.filter(accion="SURTIDO_POR_PACKING").exists())

    def test_empacar_mas_de_lo_surtido_es_400(self):
        resp = self._empacar((self.linea_m, 5))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("excede lo pendiente", self._mensaje(resp, "packing_detalle"))
        self._empacar_ok((self.linea_m, 4))
        resp = self._empacar((self.linea_m, 1))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ya no tiene cantidad pendiente", self._mensaje(resp, "packing_detalle"))
        self.linea_m.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, Decimal("4"))

    def test_trasladar_genera_transferencia_y_mueve_las_prendas(self):
        from inventarios.models import Existencia

        def cantidad(almacen):
            return sum(e.cantidad for e in Existencia.objects.filter(almacen=almacen))

        tr_alta = self.picking.transferencia_id
        trs = [tr_alta]
        for almacen in (self.otro, self.destino):  # ida y vuelta: cada traslado deja su TR
            resp = self.api.post(
                f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": almacen.pk}, format="json"
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            self.picking.refresh_from_db()
            self.assertEqual((self.picking.almacen_id, self.picking.almacen_destino_id), (almacen.pk, None))
            self.assertNotEqual(self.picking.transferencia_id, tr_alta)
            self.assertEqual(self.picking.transferencia.almacen_destino_id, almacen.pk)
            tr_alta = self.picking.transferencia_id
            trs.append(tr_alta)

        # EC-916: ``Picking.transferencia`` solo guarda la última; cada TR (alta y
        # traslados) conserva su picking en ``Transferencia.picking``.
        self.assertEqual(sorted(self.picking.transferencias.values_list("pk", flat=True)), sorted(trs))
        self.assertEqual((cantidad(self.origen), cantidad(self.otro), cantidad(self.destino)), (14, 0, 6))
        resp = self.api.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": self.destino.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)  # ya está ahí

    def test_almacen_actual_en_las_respuestas_del_picking(self):
        """EC-607: con TR y destino las prendas están en el destino; tras trasladar, en el nuevo."""
        from wms.services.picking_pipeline.pendientes import listar_folios_picking

        def actual(fila):
            return fila["almacen_actual"], fila["almacen_actual_nombre"]

        self.assertIsNotNone(self.picking.transferencia_id)
        esperado = (self.destino.pk, "Apartados")
        detalle = self.api.get(f"/api/v1/wms/pickings/{self.picking.pk}/").json()
        self.assertEqual((detalle["almacen"], actual(detalle)), (self.origen.pk, esperado))
        listado = self.api.get("/api/v1/wms/pickings/").json()
        filas = listado.get("results", listado) if isinstance(listado, dict) else listado
        self.assertEqual([actual(f) for f in filas if f["id"] == self.picking.pk], [esperado])
        self.assertEqual([actual(f) for f in listar_folios_picking(self.pedido)], [esperado])

        resp = self.api.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": self.otro.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(actual(resp.json()), (self.otro.pk, "Bordados"))
        self.assertEqual([actual(f) for f in listar_folios_picking(self.pedido)], [(self.otro.pk, "Bordados")])

    def test_la_tr_del_traslado_es_de_la_sucursal_del_picking(self):
        # #693: no la del usuario, aunque su sucursal por defecto sea otra.
        Usuario.objects.filter(pk=self.usuario.pk).update(
            sucursal_default=Sucursal.objects.create(empresa=self.empresa, codigo="NTE", nombre="Norte")
        )
        self.api.force_authenticate(Usuario.objects.get(pk=self.usuario.pk))

        resp = self.api.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": self.otro.pk}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.picking.refresh_from_db()
        self.assertEqual(self.picking.transferencia.sucursal_id, self.sucursal.pk)

    def test_tr_no_completada_no_cuenta_como_movida(self):  # #701
        from wms.models import Picking, Transferencia

        self.assertEqual(self.picking.almacen_actual, self.destino)
        Transferencia.objects.filter(pk=self.picking.transferencia_id).update(
            status=Transferencia.TransferenciaStatus.CANCELADA
        )
        self.assertEqual(Picking.objects.get(pk=self.picking.pk).almacen_actual, self.origen)

    def test_traslado_rechazado_tras_empacar(self):
        self._empacar_ok((self.linea_m, 1))
        resp = self.api.post(
            f"/api/v1/wms/pickings/{self.picking.pk}/trasladar/", {"almacen": self.otro.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_onboarding_pendiente_y_picking_totalmente_empacado(self):
        def onboarding(**params):
            resp = self.api.get("/api/v1/wms/packings/onboarding/", params)
            self.assertEqual(resp.status_code, 200, resp.content)
            return resp.json()

        self._empacar_ok((self.linea_m, 3))
        datos = onboarding(picking=self.picking.pk)
        pendientes = {d["picking_detalle"]: Decimal(d["cantidad_pendiente_empacar"]) for d in datos["packing_detalle"]}
        self.assertEqual(pendientes, {self.linea_m.pk: Decimal("1"), self.linea_g.pk: Decimal("2")})
        self.assertIn(self.picking.pk, [p["id"] for p in datos["pickings"]])  # parcial: sigue

        self._empacar_ok((self.linea_m, 1), (self.linea_g, 2))
        self.assertNotIn(self.picking.pk, [p["id"] for p in onboarding()["pickings"]])  # completo: fuera

    # --- WM-02 -------------------------------------------------------------
    def _aviso(self, packing):
        resp = self.api.get("/api/v1/wms/despachos/onboarding/", {"packing": packing["id"]})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["embarque"]["motivo"]

    def test_sin_embarque_parcial_despacha_lo_empacado(self):
        """embarque_parcial=False no bloquea: lo empacado sale aunque falte empacar."""
        primero = self._empacar_ok((self.linea_m, 4))
        self.assertEqual(self._aviso(primero), "Aviso: el pedido no admite embarque parcial y faltan 2 piezas por empacar de las 6 del pedido.")
        self.assertEqual(self._despachar(primero).status_code, 201)

        segundo = self._empacar_ok((self.linea_g, 2))
        self.assertIsNone(self._aviso(segundo))
        self.assertEqual(self._despachar(segundo).status_code, 201)

    def test_con_embarque_parcial_despacha_parcial(self):
        from ventas.models import Pedido

        Pedido.objects.filter(pk=self.pedido.pk).update(embarque_parcial=True)
        self.assertEqual(self._despachar(self._empacar_ok((self.linea_m, 1))).status_code, 201)

    def test_packing_o_linea_cancelados_no_cuentan(self):
        from wms.models import Packing, PackingDetalle

        primero = self._empacar_ok((self.linea_m, 4))
        segundo = self._empacar_ok((self.linea_g, 2))
        self.assertIsNone(self._aviso(primero))
        Packing.objects.filter(pk=segundo["id"]).update(estado="CANCELADO")
        self.assertIn("faltan 2 piezas", self._aviso(primero))

        Packing.objects.filter(pk=segundo["id"]).update(estado="PENDIENTE")
        PackingDetalle.objects.filter(packing_id=segundo["id"]).update(estado="CANCELADO")
        self.assertIn("faltan 2 piezas", self._aviso(primero))

    def test_linea_cancelada_se_rechaza_y_no_se_ofrece(self):
        from ventas.models import Pedido
        from wms.models import PackingDetalle

        Pedido.objects.filter(pk=self.pedido.pk).update(embarque_parcial=True)
        packing = self._empacar_ok((self.linea_m, 1), (self.linea_g, 1))
        cancelada = packing["packing_detalle"][0]["id"]
        PackingDetalle.objects.filter(pk=cancelada).update(estado="CANCELADO")

        resp = self._despachar(packing)
        self.assertEqual(resp.status_code, 400)
        mensaje = self._mensaje(resp, "despacho_detalle")
        self.assertIn("cancelados", mensaje)
        self.assertIsNone(self.STALE_DESPACHO_RE.search(mensaje))

        datos = self.api.get("/api/v1/wms/despachos/onboarding/", {"packing": packing["id"]}).json()
        self.assertNotIn(cancelada, [d["packing_detalle"] for d in datos["despacho_detalle"]])

    def test_muestras_fuera_de_ambos_lados(self):
        from wms.models import PackingDetalle, PickingDetalle

        primero = self._empacar_ok((self.linea_m, 4))
        # Línea de muestra empacada (hecha a mano: la muestra no tiene SKU para la TR
        # del picking). Sus 2 piezas no deben completar las 6 del pedido.
        linea_muestra = PickingDetalle.objects.create(
            picking=self.picking, pedido_detalle=self.detalle_muestra,
            cantidad_solicitada=2, cantidad_asignada=2,
        )
        PackingDetalle.objects.create(
            packing_id=primero["id"], picking_detalle=linea_muestra, cantidad_empacada=2
        )
        self.assertIn("faltan 2 piezas por empacar de las 6", self._aviso(primero))

    # --- revisión de código ------------------------------------------------
    def test_despacho_de_parte_de_un_packing(self):
        packing = self._empacar_ok((self.linea_m, 4), (self.linea_g, 2))
        solo_m = {**packing, "packing_detalle": packing["packing_detalle"][:1]}
        self.assertEqual(self._despachar(solo_m).status_code, 201)

    def test_reempacar_tras_cancelar_no_infla_el_surtido(self):
        from wms.models import Packing, PickingDetalle

        primero = self._empacar_ok((self.linea_m, 4))
        Packing.objects.filter(pk=primero["id"]).update(estado="CANCELADO")
        self._empacar_ok((self.linea_m, 4))
        self.linea_m.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, Decimal("4"))
        self.assertEqual(self.linea_m.estado, PickingDetalle.EstadoLinea.SURTIDA)

    def test_linea_de_picking_cancelada_no_cuenta_en_o1(self):
        from wms.models import PickingDetalle

        packing = self._empacar_ok((self.linea_m, 4), (self.linea_g, 2))
        PickingDetalle.objects.filter(pk=self.linea_g.pk).update(estado=PickingDetalle.EstadoLinea.CANCELADA)
        self.assertIn("faltan 2 piezas", self._aviso(packing))

    def test_otra_empresa_rechazada_antes_de_bloquear(self):
        from ventas.models import Pedido

        packing = self._empacar_ok((self.linea_m, 4), (self.linea_g, 2))
        intruso = Usuario.objects.create(
            username="ec574-x@example.com", email="ec574-x@example.com",
            empresa=Empresa.objects.create(codigo="ec574x", razon_social="Otra SA"),
        )
        api = APIClient()
        api.force_authenticate(intruso)
        with patch.object(Pedido.objects, "select_for_update", wraps=Pedido.objects.select_for_update) as lock:
            resp_despacho = api.post(
                "/api/v1/wms/despachos/",
                {"packing": packing["id"], "despacho_detalle": [{"packing_detalle": d["id"]} for d in packing["packing_detalle"]]},
                format="json",
            )
            resp_packing = api.post(
                "/api/v1/wms/packings/",
                {"picking": self.picking.pk, "packing_detalle": [{"picking_detalle": self.linea_m.pk, "cantidad_empacada": "1"}]},
                format="json",
            )
        self.assertIn(resp_despacho.status_code, (400, 404))
        self.assertIn(resp_packing.status_code, (400, 404))
        lock.assert_not_called()

    def test_pedido_inexistente_o_cancelado_al_bloquear_responde_400(self):  # #1368
        from ventas.models import Pedido
        from wms.models import Despacho, Packing

        packing = self._empacar_ok((self.linea_m, 2))
        antes = (Packing.objects.count(), Despacho.objects.count())
        with patch.object(Pedido.objects, "select_for_update", return_value=Pedido.objects.none()):
            respuestas = [self._empacar((self.linea_m, 1)), self._despachar(packing)]
        for resp in respuestas:
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(self._mensaje(resp, "pedido"), "Pedido no encontrado o sin acceso.")
        Pedido.objects.filter(pk=self.picking.pedido_id).update(estatus=Pedido.ESTATUS_CANCELADO)
        for resp in (self._empacar((self.linea_m, 1)), self._despachar(packing)):
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertIn("cancelado", self._mensaje(resp, "pedido"))
        self.assertEqual((Packing.objects.count(), Despacho.objects.count()), antes)

    def test_onboarding_de_despacho_solo_avisa(self):
        def onboarding(packing):
            resp = self.api.get("/api/v1/wms/despachos/onboarding/", {"packing": packing["id"]})
            self.assertEqual(resp.status_code, 200, resp.content)
            return resp.json()

        primero = self._empacar_ok((self.linea_m, 4))
        datos = onboarding(primero)
        self.assertEqual(
            datos["embarque"],
            {
                "embarque_parcial": False,
                "total_piezas_pedido": "6",
                "piezas_empacadas": "4",
                "piezas_por_empacar": "2",
                "despachable": True,
                "motivo": "Aviso: el pedido no admite embarque parcial y faltan 2 piezas por empacar de las 6 del pedido.",
            },
        )
        self.assertTrue(all(d["disponible_para_despacho"] for d in datos["despacho_detalle"]))
        self.assertTrue(all(not d["ya_despachado"] for d in datos["despacho_detalle"]))

        self._empacar_ok((self.linea_g, 2))
        datos = onboarding(primero)
        self.assertTrue(datos["embarque"]["despachable"])
        self.assertIsNone(datos["embarque"]["motivo"])
        self.assertEqual(datos["embarque"]["piezas_por_empacar"], "0")
        self.assertTrue(all(d["disponible_para_despacho"] for d in datos["despacho_detalle"]))


def _documentos_wms(codigo, *, superusuario=False):
    """Una empresa con un picking, un packing y una transferencia con dos tipos de línea.

    Fixture de EC-882, armado con el ORM y sin servicios: cada documento trae una
    línea de catálogo (``ProductoVariante`` con color y talla) y una de muestra
    (``VarianteProductoProduccion``, sin producto ni variante de catálogo), las
    dos formas que el PDF de WMS tiene que poder imprimir.
    """
    from catalogo.models import Color, ProductoVariante, Talla, VarianteProductoProduccion
    from inventarios.models import Almacen
    from nucleo.models import Moneda
    from terceros.models import Cliente
    from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla
    from wms.models import (
        Packing,
        PackingDetalle,
        Picking,
        PickingDetalle,
        Transferencia,
        TransferenciaDetalle,
    )

    d = {}
    d["empresa"] = Empresa.objects.create(codigo=f"wms-{codigo}", razon_social=f"WMS {codigo} SA")
    d["sucursal"] = Sucursal.objects.create(empresa=d["empresa"], codigo=f"S{codigo}", nombre=f"Sucursal {codigo}")
    d["usuario"] = Usuario.objects.create(
        username=f"wms-{codigo}@example.com",
        email=f"wms-{codigo}@example.com",
        empresa=d["empresa"],
        sucursal_default=d["sucursal"],
        is_superuser=superusuario,
        is_admin_empresa=not superusuario,
    )
    origen = Almacen.objects.create(
        empresa=d["empresa"], sucursal=d["sucursal"], codigo=f"PT{codigo}", nombre=f"Terminado {codigo}",
        permite_salida=True, permite_entrada=True,
    )
    destino = Almacen.objects.create(
        empresa=d["empresa"], sucursal=d["sucursal"], codigo=f"AP{codigo}", nombre=f"Apartados {codigo}",
        permite_salida=True, permite_entrada=True,
    )
    d["color"] = Color.objects.create(nombre=f"MARINO {codigo}", codigo="MAR", codigo_hex="#000080")
    d["talla"] = Talla.objects.create(nombre=f"M{codigo}")
    d["color_muestra"] = Color.objects.create(nombre=f"ROJO {codigo}", codigo="ROJ", codigo_hex="#FF0000")
    d["talla_muestra"] = Talla.objects.create(nombre=f"G{codigo}")
    producto = Producto.objects.create(
        tipo=_tipo_producto(), empresa=d["empresa"], nombre=f"Chamarra {codigo}", codigo=f"CH{codigo}"
    )
    d["variante"] = ProductoVariante.objects.create(
        producto=producto, empresa=d["empresa"], color=d["color"], talla=d["talla"],
        nombre=f"Chamarra {codigo} variante", sku=f"CH{codigo}-MAR-M", precio_base=Decimal("100"),
    )
    d["muestra"] = VarianteProductoProduccion.objects.create(
        empresa=d["empresa"], color=d["color_muestra"], talla=d["talla_muestra"],
        nombre=f"Muestra {codigo}", sku=f"MP-{codigo}-G",
    )
    moneda, _ = Moneda.objects.get_or_create(codigo_iso="MXN", defaults={"nombre": "Peso"})
    d["cliente"] = Cliente.objects.create(
        empresa=d["empresa"], nombre=f"Cliente {codigo}", razon_social=f"Cliente {codigo} SA de CV",
        rfc=f"CLI{codigo}010101A",
    )
    d["pedido"] = Pedido.objects.create(
        empresa=d["empresa"], sucursal=d["sucursal"], cliente=d["cliente"], moneda=moneda,
        folio=f"P-{codigo}", estatus=3, persona_pagos="Pagos", correo_facturas="p@example.com",
        telefono_pagos="8100000000", forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
    )
    linea = PedidoDetalle.objects.create(pedido=d["pedido"], producto=producto)
    talla_catalogo = PedidoDetalleTalla.objects.create(
        pedido_detalle=linea, talla=d["talla"], cantidad=2, variante=d["variante"]
    )
    linea_muestra = PedidoDetalle.objects.create(pedido=d["pedido"])
    talla_muestra = PedidoDetalleTalla.objects.create(
        pedido_detalle=linea_muestra, talla=d["talla_muestra"], cantidad=1, sku_muestra=d["muestra"].sku
    )

    d["transferencia"] = Transferencia.objects.create(
        empresa=d["empresa"], sucursal=d["sucursal"], almacen_origen=origen, almacen_destino=destino,
        folio=f"TR-{codigo}", usuario=d["usuario"],
    )
    d["tr_catalogo"] = TransferenciaDetalle.objects.create(
        transferencia=d["transferencia"], producto=producto, producto_variante=d["variante"], cantidad=2
    )
    d["tr_muestra"] = TransferenciaDetalle.objects.create(
        transferencia=d["transferencia"], variante_produccion=d["muestra"], cantidad=1
    )
    d["picking"] = Picking.objects.create(
        folio=f"PICK-{codigo}", empresa=d["empresa"], sucursal=d["sucursal"], pedido=d["pedido"],
        operador=d["usuario"], almacen=origen, almacen_destino=destino, transferencia=d["transferencia"],
        usuario=d["usuario"],
    )
    d["pk_catalogo"] = PickingDetalle.objects.create(
        picking=d["picking"], pedido_detalle=linea, pedido_detalle_talla=talla_catalogo, producto=producto,
        producto_variante=d["variante"], cantidad_solicitada=2, cantidad_asignada=2, cantidad_surtida=2,
    )
    d["pk_muestra"] = PickingDetalle.objects.create(
        picking=d["picking"], pedido_detalle=linea_muestra, pedido_detalle_talla=talla_muestra,
        variante_produccion=d["muestra"], cantidad_solicitada=1, cantidad_asignada=1, cantidad_surtida=1,
    )
    d["packing"] = Packing.objects.create(
        folio=f"PACK-{codigo}", empresa=d["empresa"], sucursal=d["sucursal"], pedido=d["pedido"],
        picking=d["picking"], operador=d["usuario"], usuario=d["usuario"],
    )
    PackingDetalle.objects.create(packing=d["packing"], picking_detalle=d["pk_catalogo"], cantidad_empacada=2)
    PackingDetalle.objects.create(packing=d["packing"], picking_detalle=d["pk_muestra"], cantidad_empacada=1)
    return d


def _api_de(user):
    api = APIClient()
    api.force_authenticate(user)
    return api


class DocumentosWmsAlcanceSuperusuarioTests(TestCase):
    """EC-882: picking, packing y transferencia acotan al superusuario a su empresa activa.

    Mismo criterio que Producción desde EC-880 (``wms/scope.py``): antes el
    superusuario veía en list y retrieve los documentos de todas las empresas.
    """

    RECURSOS = {"pickings": "picking", "packings": "packing", "transferencias": "transferencia"}

    @classmethod
    def setUpTestData(cls):
        cls.propia = _documentos_wms("A", superusuario=True)
        cls.ajena = _documentos_wms("B")
        cls.superusuario = cls.propia["usuario"]

    def _ids(self, user, recurso):
        resp = _api_de(user).get(f"/api/v1/wms/{recurso}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return [fila["id"] for fila in resp.json()]

    def test_superusuario_solo_lista_su_empresa_activa(self):
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(self.superusuario, recurso), [self.propia[clave].pk])

    def test_superusuario_recibe_404_en_el_detalle_de_otra_empresa(self):
        api = _api_de(self.superusuario)
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                propio = api.get(f"/api/v1/wms/{recurso}/{self.propia[clave].pk}/")
                self.assertEqual(propio.status_code, 200, propio.content)
                ajeno = api.get(f"/api/v1/wms/{recurso}/{self.ajena[clave].pk}/")
                self.assertEqual(ajeno.status_code, 404, ajeno.content)

    def test_superusuario_sigue_a_su_empresa_activa_al_cambiarla(self):
        """Tras ``POST /api/v1/nucleo/cambiar-empresa/`` (EC-843) list, retrieve y
        ``trasladar`` siguen a la empresa nueva y ya no ven la anterior."""
        resp = _api_de(self.superusuario).post(
            "/api/v1/nucleo/cambiar-empresa/", {"empresa": self.ajena["empresa"].pk}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # El JWT sólo lleva el id: cada petición vuelve a leer al usuario de la BD.
        user = Usuario.objects.get(pk=self.superusuario.pk)
        self.assertEqual(user.empresa_id, self.ajena["empresa"].pk)
        api = _api_de(user)
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(user, recurso), [self.ajena[clave].pk])
                nueva = api.get(f"/api/v1/wms/{recurso}/{self.ajena[clave].pk}/")
                self.assertEqual(nueva.status_code, 200, nueva.content)
                anterior = api.get(f"/api/v1/wms/{recurso}/{self.propia[clave].pk}/")
                self.assertEqual(anterior.status_code, 404, anterior.content)
        resp = api.post(
            f"/api/v1/wms/pickings/{self.propia['picking'].pk}/trasladar/",
            {"almacen": self.propia["picking"].almacen_destino_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_superusuario_sin_empresa_no_ve_nada(self):
        Usuario.objects.filter(pk=self.superusuario.pk).update(empresa=None)
        user = Usuario.objects.get(pk=self.superusuario.pk)
        for recurso in self.RECURSOS:
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(user, recurso), [])

    def test_trasladar_un_picking_de_otra_empresa_es_404(self):
        resp = _api_de(self.superusuario).post(
            f"/api/v1/wms/pickings/{self.ajena['picking'].pk}/trasladar/",
            {"almacen": self.ajena["picking"].almacen_destino_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_admin_de_empresa_conserva_su_alcance(self):
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(self.ajena["usuario"], recurso), [self.ajena[clave].pk])

    def _usuario_normal(self, sucursal, codigo):
        """Usuario de la empresa B sin ``is_admin_empresa`` ni superusuario."""
        return Usuario.objects.create(
            username=f"wms-normal-{codigo}@example.com",
            email=f"wms-normal-{codigo}@example.com",
            empresa=self.ajena["empresa"],
            sucursal_default=sucursal,
        )

    def test_usuario_normal_ve_su_sucursal_y_no_otra_empresa(self):
        """Sin cambios para el usuario normal: su sucursal sí, la empresa A es 404 (también en trasladar)."""
        user = self._usuario_normal(self.ajena["sucursal"], "b")
        api = _api_de(user)
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(user, recurso), [self.ajena[clave].pk])
                propio = api.get(f"/api/v1/wms/{recurso}/{self.ajena[clave].pk}/")
                self.assertEqual(propio.status_code, 200, propio.content)
                ajeno = api.get(f"/api/v1/wms/{recurso}/{self.propia[clave].pk}/")
                self.assertEqual(ajeno.status_code, 404, ajeno.content)
        resp = api.post(
            f"/api/v1/wms/pickings/{self.propia['picking'].pk}/trasladar/",
            {"almacen": self.propia["picking"].almacen_destino_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_usuario_normal_de_otra_sucursal_no_ve_nada(self):
        """Dentro de su empresa sigue acotado a ``sucursales_permitidas()``: otra sucursal es 404."""
        otra = Sucursal.objects.create(empresa=self.ajena["empresa"], codigo="SB2", nombre="Sucursal B2")
        user = self._usuario_normal(otra, "b2")
        api = _api_de(user)
        for recurso, clave in self.RECURSOS.items():
            with self.subTest(recurso=recurso):
                self.assertEqual(self._ids(user, recurso), [])
                resp = api.get(f"/api/v1/wms/{recurso}/{self.ajena[clave].pk}/")
                self.assertEqual(resp.status_code, 404, resp.content)
        resp = api.post(
            f"/api/v1/wms/pickings/{self.ajena['picking'].pk}/trasladar/",
            {"almacen": self.ajena["picking"].almacen_destino_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404, resp.content)


class DocumentosWmsLineasVarianteTests(TestCase):
    """EC-882: SKU, color y talla en los renglones de picking, packing y transferencia.

    Una línea de catálogo los lee de su ``ProductoVariante``; una de muestra, de su
    ``VarianteProductoProduccion``, y además trae un ``producto_nombre`` usable en
    vez de ``null`` (antes una TR nacida de un picking con muestra llegaba con
    producto, variante y los dos nombres vacíos).
    """

    @classmethod
    def setUpTestData(cls):
        cls.d = _documentos_wms("L")

    def _detalle(self, recurso, documento):
        resp = _api_de(self.d["usuario"]).get(f"/api/v1/wms/{recurso}/{documento.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _esperado_catalogo(self):
        d = self.d
        return {
            "sku": d["variante"].sku,
            "color_id": d["color"].pk,
            "color_nombre": d["color"].nombre,
            "talla_id": d["talla"].pk,
            "talla_nombre": d["talla"].nombre,
            "sku_muestra": None,
        }

    def _esperado_muestra(self):
        d = self.d
        return {
            "sku": d["muestra"].sku,
            "color_id": d["color_muestra"].pk,
            "color_nombre": d["color_muestra"].nombre,
            "talla_id": d["talla_muestra"].pk,
            "talla_nombre": d["talla_muestra"].nombre,
            "sku_muestra": d["muestra"].sku,
            "producto_nombre": d["muestra"].nombre,
            "producto": None,
            "producto_variante": None,
            "producto_variante_nombre": None,
            "variante_produccion": d["muestra"].pk,
        }

    def _assert_lineas(self, lineas):
        catalogo, muestra = lineas
        self.assertEqual({k: catalogo[k] for k in self._esperado_catalogo()}, self._esperado_catalogo())
        self.assertEqual({k: muestra[k] for k in self._esperado_muestra()}, self._esperado_muestra())

    def test_picking(self):
        self._assert_lineas(self._detalle("pickings", self.d["picking"])["picking_detalle"])

    def test_packing(self):
        self._assert_lineas(self._detalle("packings", self.d["packing"])["packing_detalle"])

    def test_transferencia(self):
        self._assert_lineas(self._detalle("transferencias", self.d["transferencia"])["transferencia_detalle"])

    def test_los_listados_de_picking_y_packing_traen_los_mismos_campos(self):
        api = _api_de(self.d["usuario"])
        (picking,) = api.get("/api/v1/wms/pickings/").json()
        self._assert_lineas(picking["picking_detalle"])
        (packing,) = api.get("/api/v1/wms/packings/").json()
        self._assert_lineas(packing["packing_detalle"])

    def test_packing_sin_talla_de_pedido_trae_talla_id_en_null(self):
        """``talla_id`` viaja siempre en packing: sin variante ni talla de pedido, ``null`` (antes se omitía)."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(
            pedido_detalle_talla=None, producto_variante=None
        )
        catalogo, muestra = self._detalle("packings", self.d["packing"])["packing_detalle"]
        self.assertIn("talla_id", catalogo)
        self.assertEqual((catalogo["talla_id"], catalogo["talla_nombre"]), (None, None))
        self.assertEqual(catalogo["producto_nombre"], self.d["variante"].producto.nombre)
        self.assertEqual(muestra["talla_id"], self.d["talla_muestra"].pk)

    def _campos(self, linea):
        return {k: linea[k] for k in ("sku", "color_id", "color_nombre", "talla_id", "talla_nombre", "sku_muestra")}

    def _lineas_catalogo(self):
        """El renglón de catálogo en el detalle de picking y en el de packing."""
        return {
            "picking": self._detalle("pickings", self.d["picking"])["picking_detalle"][0],
            "packing": self._detalle("packings", self.d["packing"])["packing_detalle"][0],
        }

    def test_linea_de_catalogo_sin_variante_toma_la_talla_del_pedido(self):
        """Sin ``producto_variante`` ni muestra, ``talla_*`` sale de ``PedidoDetalleTalla.talla``;
        ``sku`` y ``color_*`` quedan en ``null`` (antes la talla también llegaba en ``null``)."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(producto_variante=None)
        for documento, linea in self._lineas_catalogo().items():
            with self.subTest(documento=documento):
                self.assertEqual(
                    self._campos(linea),
                    {
                        "sku": None, "color_id": None, "color_nombre": None,
                        "talla_id": self.d["talla"].pk, "talla_nombre": self.d["talla"].nombre,
                        "sku_muestra": None,
                    },
                )
                self.assertEqual(linea["producto_nombre"], self.d["variante"].producto.nombre)

    def test_pedido_editado_despues_del_picking_no_cambia_el_renglon(self):
        """SKU, color y talla salen de la variante surtida en la línea, no de la que hoy
        tenga la talla del pedido: reasignarla o dejarla en ``null`` no mueve el renglón."""
        from catalogo.models import Color, ProductoVariante, Talla
        from ventas.models import PedidoDetalleTalla

        otra = ProductoVariante.objects.create(
            producto=self.d["variante"].producto, empresa=self.d["empresa"],
            color=Color.objects.create(nombre="VERDE L", codigo="VER", codigo_hex="#00FF00"),
            talla=Talla.objects.create(nombre="XGL"), nombre="Otra variante", sku="CHL-VER-XG",
            precio_base=Decimal("100"),
        )
        esperado = self._esperado_catalogo()
        talla_pedido = PedidoDetalleTalla.objects.filter(pk=self.d["pk_catalogo"].pedido_detalle_talla_id)
        for variante_del_pedido in (otra, None):
            talla_pedido.update(variante=variante_del_pedido)
            for documento, linea in self._lineas_catalogo().items():
                with self.subTest(variante_del_pedido=variante_del_pedido, documento=documento):
                    self.assertEqual(self._campos(linea), esperado)

    def test_linea_con_las_dos_variantes_da_dos_skus(self):
        """Con variante de catálogo y muestra, ``sku`` es el de catálogo (lo surtido) y
        ``sku_muestra`` el de la muestra: pueden diferir, como dice la documentación."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(variante_produccion=self.d["muestra"])
        for documento, linea in self._lineas_catalogo().items():
            with self.subTest(documento=documento):
                self.assertEqual(self._campos(linea), {**self._esperado_catalogo(), "sku_muestra": self.d["muestra"].sku})
                self.assertNotEqual(linea["sku"], linea["sku_muestra"])

    def test_linea_sin_variante_deja_los_campos_en_null(self):
        from wms.models import TransferenciaDetalle

        TransferenciaDetalle.objects.filter(pk=self.d["tr_catalogo"].pk).update(producto_variante=None)
        catalogo, _ = self._detalle("transferencias", self.d["transferencia"])["transferencia_detalle"]
        self.assertEqual(
            {k: catalogo[k] for k in ("sku", "color_id", "color_nombre", "talla_id", "talla_nombre")},
            dict.fromkeys(("sku", "color_id", "color_nombre", "talla_id", "talla_nombre")),
        )
        self.assertEqual(catalogo["producto_nombre"], self.d["tr_catalogo"].producto.nombre)

    def test_las_consultas_no_crecen_con_los_renglones(self):
        """Duplicar los renglones no agrega consultas en ningún detalle ni listado."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from wms.models import PackingDetalle, PickingDetalle, TransferenciaDetalle

        rutas = (
            f"/api/v1/wms/pickings/{self.d['picking'].pk}/",
            f"/api/v1/wms/packings/{self.d['packing'].pk}/",
            f"/api/v1/wms/transferencias/{self.d['transferencia'].pk}/",
            "/api/v1/wms/pickings/",
            "/api/v1/wms/packings/",
        )
        api = _api_de(self.d["usuario"])

        def consultas():
            conteo = {}
            for ruta in rutas:
                with CaptureQueriesContext(connection) as ctx:
                    self.assertEqual(api.get(ruta).status_code, 200)
                conteo[ruta] = len(ctx.captured_queries)
            return conteo

        antes = consultas()
        for modelo, filtro in (
            (PickingDetalle, {"picking": self.d["picking"]}),
            (PackingDetalle, {"packing": self.d["packing"]}),
            (TransferenciaDetalle, {"transferencia": self.d["transferencia"]}),
        ):
            for fila in list(modelo.objects.filter(**filtro)):
                fila.pk = None
                fila.save()
        self.assertEqual(consultas(), antes)


class DocumentosWmsClienteTests(TestCase):
    """EC-882: picking y packing traen ``pedido_vinculado`` con el cliente del pedido.

    Misma forma y regla que los detalles de Producción (EC-880,
    ``armar_pedido_vinculado``): ``{id, folio, cliente: {id, nombre, razon_social,
    rfc}}``, con la foto del cliente del pedido o, sin foto, el cliente vivo.
    """

    RECURSOS = {"pickings": "picking", "packings": "packing"}

    @classmethod
    def setUpTestData(cls):
        cls.d = _documentos_wms("C")

    def _vinculado(self, recurso):
        documento = self.d[self.RECURSOS[recurso]]
        resp = _api_de(self.d["usuario"]).get(f"/api/v1/wms/{recurso}/{documento.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        cuerpo = resp.json()
        # Lo que ya había no cambia.
        self.assertEqual((cuerpo["pedido"], cuerpo["pedido_folio"]), (self.d["pedido"].pk, self.d["pedido"].folio))
        return cuerpo["pedido_vinculado"]

    def test_produccion_conserva_su_ruta_de_importacion(self):
        """El helper vive en ``ventas``; la ruta de Producción (EC-880) sigue dando el mismo."""
        from produccion.services import orden_bordado_field_filter_service as ruta_produccion
        from ventas.services import pedido_vinculado

        self.assertIs(ruta_produccion.armar_pedido_vinculado, pedido_vinculado.armar_pedido_vinculado)
        self.assertIs(ruta_produccion.cliente_de_pedido, pedido_vinculado.cliente_de_pedido)

    def test_sin_foto_sale_el_cliente_vivo(self):
        cliente = self.d["cliente"]
        for recurso in self.RECURSOS:
            with self.subTest(recurso=recurso):
                self.assertEqual(
                    self._vinculado(recurso),
                    {
                        "id": self.d["pedido"].pk,
                        "folio": self.d["pedido"].folio,
                        "cliente": {
                            "id": cliente.pk,
                            "nombre": cliente.nombre,
                            "razon_social": cliente.razon_social,
                            "rfc": cliente.rfc,
                        },
                    },
                )

    def test_con_foto_sale_lo_que_se_vendio(self):
        from ventas.models import Pedido

        Pedido.objects.filter(pk=self.d["pedido"].pk).update(
            cliente_nombre="Foto", cliente_razon_social="Foto SA", cliente_rfc=None
        )
        for recurso in self.RECURSOS:
            with self.subTest(recurso=recurso):
                self.assertEqual(
                    self._vinculado(recurso)["cliente"],
                    {"id": self.d["cliente"].pk, "nombre": "Foto", "razon_social": "Foto SA", "rfc": None},
                )

    def test_los_listados_lo_traen_sin_consultas_por_documento(self):
        """Un segundo documento, de otro pedido y otro cliente, no agrega consultas al listado."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from terceros.models import Cliente
        from ventas.models import Pedido
        from wms.models import Packing, Picking

        api = _api_de(self.d["usuario"])

        def listar():
            conteo, cuerpos = {}, {}
            for recurso in self.RECURSOS:
                with CaptureQueriesContext(connection) as ctx:
                    resp = api.get(f"/api/v1/wms/{recurso}/")
                conteo[recurso], cuerpos[recurso] = len(ctx.captured_queries), resp.json()
            return conteo, cuerpos

        antes, cuerpos = listar()
        for recurso in self.RECURSOS:
            (fila,) = cuerpos[recurso]
            self.assertEqual(fila["pedido_vinculado"]["cliente"]["id"], self.d["cliente"].pk)

        otro_cliente = Cliente.objects.create(empresa=self.d["empresa"], nombre="Otro cliente")
        pedido = Pedido.objects.get(pk=self.d["pedido"].pk)
        pedido.pk, pedido.folio, pedido.cliente = None, "P-C2", otro_cliente
        pedido.save()
        picking = Picking.objects.get(pk=self.d["picking"].pk)
        picking.pk, picking.folio, picking.pedido = None, "PICK-C2", pedido
        picking.save()
        packing = Packing.objects.get(pk=self.d["packing"].pk)
        packing.pk, packing.folio, packing.pedido, packing.picking = None, "PACK-C2", pedido, picking
        packing.save()

        despues, cuerpos = listar()
        self.assertEqual(despues, antes)
        for recurso in self.RECURSOS:
            self.assertEqual(
                {fila["pedido_vinculado"]["cliente"]["nombre"] for fila in cuerpos[recurso]},
                {self.d["cliente"].nombre, "Otro cliente"},
            )


SURTIDO_URL = "/api/v1/wms/surtido-pedidos/"


class SurtidoPedidosTests(TestCase):
    """``/api/v1/wms/surtido-pedidos/`` (#1223) y la resta de surtido en
    ``disponibilidad`` (#1251). Reglas en el docstring de ``wms/api/surtido.py``.

    Fixture ``_documentos_wms``: pedido con una talla de catálogo (2 pzas, 2
    surtidas y empacadas) y una de muestra (1 pza, 1 surtida y empacada).
    """

    @classmethod
    def setUpTestData(cls):
        cls.d = _documentos_wms("srt")
        cls.otra = _documentos_wms("sr2")  # Producto.codigo es de 5: "CH" + codigo

    def setUp(self):
        self.api = _api_de(self.d["usuario"])

    def _detalle(self):
        r = self.api.get(f"{SURTIDO_URL}{self.d['pedido'].pk}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _suma(self, data, campo):
        return sum((Decimal(str(x[campo])) for x in data["renglones"]), Decimal("0"))

    def test_encabezado_igual_a_la_suma_de_renglones(self):
        data = self._detalle()
        for campo in ("pedidas", "surtidas", "comprometidas", "empacadas", "despachadas"):
            self.assertEqual(Decimal(str(data[campo])), self._suma(data, campo), campo)
        self.assertEqual(data["etapa"], "SURTIDO")

    def test_picking_sin_talla_no_cuenta(self):
        """#1246: un renglón de picking sin ``pedido_detalle_talla`` no suma al encabezado."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.create(
            picking=self.d["picking"], pedido_detalle=self.d["pk_catalogo"].pedido_detalle,
            pedido_detalle_talla=None, cantidad_solicitada=5, cantidad_surtida=5,
        )
        data = self._detalle()
        self.assertEqual(Decimal(str(data["surtidas"])), Decimal("3"))
        self.assertEqual(Decimal(str(data["surtidas"])), self._suma(data, "surtidas"))

    def test_talla_sobre_surtida_no_compensa_a_otra(self):
        """#1247: comprometido del pedido = Σ por talla de max(pedida − surtida, 0)."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(cantidad_surtida=5)  # 2 pedidas
        PickingDetalle.objects.filter(pk=self.d["pk_muestra"].pk).update(cantidad_surtida=0)  # 1 pedida
        data = self._detalle()
        self.assertEqual(Decimal(str(data["comprometidas"])), Decimal("1"))
        self.assertEqual(self._suma(data, "comprometidas"), Decimal("1"))
        r = self.api.get(SURTIDO_URL, {"pendientes": "1"})
        self.assertIn(self.d["pedido"].pk, [x["id"] for x in r.data["results"]])

    def test_etapa_por_talla_con_una_sobre_surtida(self):
        """#1290: Σ surtidas (5) ≥ Σ pedidas (3), pero a la muestra le falta 1: PARCIAL."""
        from wms.models import PickingDetalle

        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(cantidad_surtida=5)  # 2 pedidas
        PickingDetalle.objects.filter(pk=self.d["pk_muestra"].pk).update(cantidad_surtida=0)  # 1 pedida
        data = self._detalle()
        self.assertEqual(data["etapa"], "PARCIAL")
        self.assertEqual({x["etapa"] for x in data["renglones"]}, {"SURTIDO", "SIN_SURTIR"})
        fila = next(x for x in self.api.get(SURTIDO_URL).data["results"] if x["id"] == self.d["pedido"].pk)
        self.assertEqual(fila["etapa"], "PARCIAL")

    def test_muestra_trae_nombre_externo(self):
        """#1248."""
        from ventas.models import PedidoDetalle

        PedidoDetalle.objects.filter(pk=self.d["pk_muestra"].pedido_detalle_id).update(producto_nombre_externo="Bata muestra")
        renglon = next(x for x in self._detalle()["renglones"] if x["pedido_detalle_id"] == self.d["pk_muestra"].pedido_detalle_id)
        self.assertEqual(renglon["producto_nombre"], "Bata muestra")
        self.assertEqual(renglon["producto_nombre_externo"], "Bata muestra")

    def test_salidas_manuales_netas_activas_y_sin_despacho(self):
        """#1249 / #1250."""
        from inventarios.models import MovimientoInventario, MovimientoInventarioDetalle
        from wms.models import Despacho

        comun = dict(empresa=self.d["empresa"], sucursal=self.d["sucursal"], pedido=self.d["pedido"], usuario=self.d["usuario"])
        salida = MovimientoInventario.objects.create(tipo_movimiento="SALIDA", **comun)
        MovimientoInventarioDetalle.objects.create(movimiento_inventario=salida, cantidad=5)
        self.assertEqual(Decimal(str(self._detalle()["salidas_manuales"])), Decimal("5"))
        entrada = MovimientoInventario.objects.create(tipo_movimiento="ENTRADA", **comun)
        MovimientoInventarioDetalle.objects.create(movimiento_inventario=entrada, cantidad=2)
        self.assertEqual(Decimal(str(self._detalle()["salidas_manuales"])), Decimal("3"))
        MovimientoInventario.objects.filter(pk=salida.pk).update(activo=False)
        self.assertEqual(Decimal(str(self._detalle()["salidas_manuales"])), Decimal("0"))  # piso en 0
        despacho = Despacho.objects.create(packing=self.d["packing"])
        MovimientoInventario.objects.filter(pk=salida.pk).update(activo=True, despacho=despacho)
        self.assertEqual(Decimal(str(self._detalle()["salidas_manuales"])), Decimal("0"))

    def test_filtros_y_validaciones(self):
        self.assertEqual(self.api.get(SURTIDO_URL, {"sucursal": "²"}).status_code, 400)  # #1252
        self.assertEqual(self.api.get(SURTIDO_URL, {"sucursal": "x"}).status_code, 400)
        self.assertEqual(self.api.get(SURTIDO_URL, {"estatus": "a,b"}).status_code, 400)
        r = self.api.get(SURTIDO_URL, {"q": self.d["pedido"].folio, "sucursal": self.d["sucursal"].pk})
        self.assertEqual([x["id"] for x in r.data["results"]], [self.d["pedido"].pk])

    def test_alcance_multi_tenant(self):
        r = self.api.get(SURTIDO_URL, {"estatus": "todos"})
        self.assertNotIn(self.otra["pedido"].pk, [x["id"] for x in r.data["results"]])
        self.assertEqual(self.api.get(f"{SURTIDO_URL}{self.otra['pedido'].pk}/").status_code, 404)

    def test_consultas_constantes_por_pagina(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as una:
            self.api.get(SURTIDO_URL, {"estatus": "todos"})
        _documentos_wms("sr3")
        from ventas.models import Pedido

        Pedido.objects.filter(folio="P-sr3").update(empresa=self.d["empresa"])
        with CaptureQueriesContext(connection) as dos:
            self.api.get(SURTIDO_URL, {"estatus": "todos"})
        self.assertEqual(len(una), len(dos))

    def test_disponibilidad_resta_lo_surtido_sin_negativos(self):
        from produccion.services.pre_orden_service import disponibilidad
        from wms.models import PickingDetalle

        variante = self.d["variante"].pk
        base = disponibilidad(self.d["empresa"].pk, self.d["sucursal"].pk, [variante])[variante]
        self.assertEqual(base["comprometido"], Decimal("0"))  # 2 pedidas, 2 surtidas
        PickingDetalle.objects.filter(pk=self.d["pk_catalogo"].pk).update(cantidad_surtida=9)
        sobre = disponibilidad(self.d["empresa"].pk, self.d["sucursal"].pk, [variante])[variante]
        self.assertEqual(sobre["comprometido"], Decimal("0"))


class SurtidoPedidosPestanasEC988Tests(TestCase):
    """EC-988: "Por surtir" y "Surtidos" de Pedidos WMS en ``surtido-pedidos``.

    ``?por_surtir=1`` usa la misma expresión que el onboarding de picking
    (``pedidos_por_surtir_q``); ``?completo=1`` es ``pedidas > 0`` y
    ``comprometidas = 0``; ``?etapa=`` repite ``_etapa`` en SQL; ``filtros/`` y
    ``resumen/`` van sobre el mismo alcance.
    """

    ONBOARDING = "/api/v1/wms/pickings/onboarding/"

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla, VarianteProductoProduccion
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente
        from ventas.models import PedidoDetalle, PedidoDetalleTalla

        cls.empresa = Empresa.objects.create(codigo="ec988", razon_social="EC988 SA")
        cls.matriz = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ988", nombre="Matriz 988")
        cls.norte = Sucursal.objects.create(empresa=cls.empresa, codigo="NTE988", nombre="Norte 988")
        cls.usuario = Usuario.objects.create(
            username="ec988@example.com", email="ec988@example.com", empresa=cls.empresa,
            sucursal_default=cls.matriz, is_admin_empresa=True,
        )
        cls.almacen = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.matriz, codigo="PT988", nombre="Terminado 988", permite_salida=True,
        )
        # Para que ``programaciones`` traiga ``almacen_sugerido`` (APARTADO → APARTADOS).
        cls.apartados = Almacen.objects.create(
            empresa=cls.empresa, sucursal=cls.matriz, codigo="AP988", nombre="APARTADOS", permite_entrada=True,
        )
        cls.moneda, _ = Moneda.objects.get_or_create(codigo_iso="MXN", defaults={"nombre": "Peso"})
        cls.beta = Cliente.objects.create(empresa=cls.empresa, nombre="Beta 988")
        cls.alfa = Cliente.objects.create(empresa=cls.empresa, nombre="Alfa 988")
        cls.producto = Producto.objects.create(tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera 988", codigo="PL988")
        cls.talla = Talla.objects.create(nombre="M988")

        p = cls._pedido
        # Por surtir.
        cls.pendiente, _ = p("A-01", destinos=("APARTADO", "BORDADO"), cliente=cls.alfa)
        cls.en_proceso, t = p("A-02", estatus=4, destinos=("BORDADO",), sucursal=cls.norte)
        cls._picking(cls.en_proceso, t, 1)  # 1 de 2: PARCIAL
        cls.cancelado, t = p("A-03")
        cls._picking(cls.cancelado, t, 2, estado_picking="Cancelado")  # no cuenta
        cls.linea_cancelada, t = p("A-04")
        cls._picking(cls.linea_cancelada, t, 2, estado_linea="CANCELADA")  # no cuenta
        cls.muestra_con_sku, _ = p("A-05", talla=False)
        linea = PedidoDetalle.objects.create(pedido=cls.muestra_con_sku, producto_nombre_externo="Muestra con SKU")
        PedidoDetalleTalla.objects.create(pedido_detalle=linea, talla=cls.talla, cantidad=1)
        VarianteProductoProduccion.objects.create(
            empresa=cls.empresa, pedido_detalle=linea, talla=cls.talla, nombre="Muestra 988", sku="MP-988-A05",
        )
        # Fuera de "Por surtir".
        cls.sin_programacion, _ = p("B-01", destinos=())
        cls.retenido, _ = p("B-02", retenido=True)
        cls.borrador, _ = p("B-03", estatus=1)
        cls.muestra_sin_sku, _ = p("B-04", talla=False)  # su único pendiente: muestra sin SKU
        linea = PedidoDetalle.objects.create(pedido=cls.muestra_sin_sku, producto_nombre_externo="Muestra sin SKU")
        PedidoDetalleTalla.objects.create(pedido_detalle=linea, talla=cls.talla, cantidad=1)
        cls.vacio, _ = p("B-05", talla=False)  # pedidas = 0
        # Surtidos completos (cualquier estatus).
        cls.surtido, t = p("C-01", cliente=cls.alfa)
        cls._picking(cls.surtido, t, 2)
        cls.surtido_borrador, t = p("C-02", estatus=1, destinos=())
        cls._picking(cls.surtido_borrador, t, 2)
        cls.despachado, t = p("C-03")
        cls._despachar(cls.despachado, cls._picking(cls.despachado, t, 2), 2)

        cls.POR_SURTIR = {cls.pendiente.pk, cls.en_proceso.pk, cls.cancelado.pk, cls.linea_cancelada.pk,
                          cls.muestra_con_sku.pk}
        cls.COMPLETOS = {cls.surtido.pk, cls.surtido_borrador.pk, cls.despachado.pk}

        # Otra empresa con un pedido por surtir y uno completo: nunca visibles aquí.
        cls.otra = _documentos_wms("988")
        cls.otra_admin = cls.otra["usuario"]

    @classmethod
    def _pedido(cls, folio, *, estatus=3, destinos=("APARTADO",), retenido=False, cliente=None, sucursal=None,
                talla=True):
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

        pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=sucursal or cls.matriz, cliente=cliente or cls.beta, moneda=cls.moneda,
            folio=f"P988-{folio}", estatus=estatus, retenido=retenido,
            cliente_nombre=(cliente or cls.beta).nombre,
            programacion_conf={"programaciones": [{"destino": d, "cantidad": 2, "comentarios": ""} for d in destinos]},
            persona_pagos="Pagos", correo_facturas="ec988@example.com", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        if not talla:
            return pedido, None
        linea = PedidoDetalle.objects.create(pedido=pedido, producto=cls.producto)
        return pedido, PedidoDetalleTalla.objects.create(pedido_detalle=linea, talla=cls.talla, cantidad=2)

    @classmethod
    def _picking(cls, pedido, talla, cantidad, *, estado_picking="Completado", estado_linea="SURTIDA"):
        from wms.models import Picking, PickingDetalle

        picking = Picking.objects.create(
            folio=f"PK-{pedido.folio}", empresa=cls.empresa, sucursal=pedido.sucursal, pedido=pedido,
            operador=cls.usuario, almacen=cls.almacen, usuario=cls.usuario, estado=estado_picking,
            programacion_destino="APARTADO",
        )
        return PickingDetalle.objects.create(
            picking=picking, pedido_detalle=talla.pedido_detalle, pedido_detalle_talla=talla, producto=cls.producto,
            cantidad_solicitada=cantidad, cantidad_asignada=cantidad, cantidad_surtida=cantidad, estado=estado_linea,
        )

    @classmethod
    def _despachar(cls, pedido, picking_detalle, cantidad):
        from wms.models import Despacho, DespachoDetalle, Packing, PackingDetalle

        packing = Packing.objects.create(
            folio=f"PA-{pedido.folio}", empresa=cls.empresa, sucursal=pedido.sucursal, pedido=pedido,
            picking=picking_detalle.picking, operador=cls.usuario, usuario=cls.usuario,
        )
        detalle = PackingDetalle.objects.create(packing=packing, picking_detalle=picking_detalle, cantidad_empacada=cantidad)
        despacho = Despacho.objects.create(packing=packing)
        DespachoDetalle.objects.create(despacho=despacho, packing_detalle=detalle)

    def setUp(self):
        self.api = _api_de(self.usuario)

    def _lista(self, api=None, **params):
        params.setdefault("page_size", 200)
        r = (api or self.api).get(SURTIDO_URL, params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _ids(self, **params):
        return [x["id"] for x in self._lista(**params)["results"]]

    # --- Por surtir -----------------------------------------------------------

    def test_por_surtir_es_el_conjunto_del_onboarding(self):
        """Mismos pedidos, mismo orden y mismas ``programaciones``/``sucursal_nombre``."""
        onboarding = self.api.get(self.ONBOARDING, {"pedidos_limit": 500}).json()["pedidos"]
        data = self._lista(por_surtir=1)
        self.assertEqual(data["count"], len(self.POR_SURTIR))
        self.assertEqual({x["id"] for x in data["results"]}, self.POR_SURTIR)
        self.assertEqual([x["id"] for x in data["results"]], [p["id"] for p in onboarding])
        por_id = {p["id"]: p for p in onboarding}
        for fila in data["results"]:
            self.assertEqual(fila["programaciones"], por_id[fila["id"]]["programaciones"], fila["folio"])
            self.assertEqual(fila["sucursal_nombre"], por_id[fila["id"]]["sucursal_nombre"], fila["folio"])
        fila = next(x for x in data["results"] if x["id"] == self.pendiente.pk)
        self.assertEqual(fila["programaciones"][0]["almacen_sugerido"], {"id": self.apartados.pk, "nombre": "APARTADOS"})

    def test_por_surtir_fija_el_estatus(self):
        self.assertEqual(set(self._ids(por_surtir=1, estatus="1")), self.POR_SURTIR)
        self.assertEqual(set(self._ids(por_surtir="true", estatus="todos")), self.POR_SURTIR)

    def test_sin_por_surtir_la_fila_no_cambia(self):
        fila = self._lista()["results"][0]
        self.assertNotIn("programaciones", fila)
        self.assertNotIn("sucursal_nombre", fila)
        # Default intacto: estatus 3,4 con o sin pendiente.
        self.assertIn(self.sin_programacion.pk, self._ids())
        self.assertNotIn(self.borrador.pk, self._ids())

    def test_muestra_sin_sku_queda_fuera_de_ambas_pestanas(self):
        """Decisión de EC-988: pendiente sólo en muestras sin SKU → ni por surtir ni completo."""
        self.assertNotIn(self.muestra_sin_sku.pk, self._ids(por_surtir=1))
        self.assertNotIn(self.muestra_sin_sku.pk, self._ids(estatus="todos", completo=1))
        self.assertIn(self.muestra_sin_sku.pk, self._ids(pendientes=1))

    # --- Surtidos -------------------------------------------------------------

    def test_completo_de_cualquier_estatus(self):
        self.assertEqual(set(self._ids(estatus="todos", completo=1)), self.COMPLETOS)
        # Con el estatus por omisión, el borrador surtido no entra.
        self.assertEqual(set(self._ids(completo=1)), self.COMPLETOS - {self.surtido_borrador.pk})
        self.assertNotIn(self.vacio.pk, self._ids(estatus="todos", completo=1))  # pedidas = 0
        for fila in self._lista(estatus="todos", completo=1)["results"]:
            self.assertGreater(fila["pedidas"], 0)
            self.assertEqual(fila["comprometidas"], 0)

    def test_etapa_igual_a_la_de_la_fila(self):
        filas = self._lista(estatus="todos")["results"]
        for etapa in ("SIN_SURTIR", "PARCIAL", "SURTIDO", "DESPACHADO"):
            esperados = {x["id"] for x in filas if x["etapa"] == etapa}
            self.assertTrue(esperados, etapa)  # el fixture cubre las cuatro
            self.assertEqual(set(self._ids(estatus="todos", etapa=etapa)), esperados, etapa)
        varias = {x["id"] for x in filas if x["etapa"] in ("SURTIDO", "DESPACHADO")}
        self.assertEqual(set(self._ids(estatus="todos", etapa="SURTIDO,DESPACHADO")), varias)
        self.assertEqual(varias, self.COMPLETOS)
        self.assertEqual(self.api.get(SURTIDO_URL, {"etapa": "surtido"}).status_code, 400)

    # --- Filtros de columna ---------------------------------------------------

    def test_destino_alguna_programacion(self):
        self.assertEqual(set(self._ids(por_surtir=1, destino="BORDADO")), {self.pendiente.pk, self.en_proceso.pk})
        self.assertEqual(
            set(self._ids(por_surtir=1, destino="BORDADO,APARTADO")), self.POR_SURTIR,
        )
        self.assertEqual(self._ids(por_surtir=1, destino="EMBARQUE"), [])
        r = self.api.get(SURTIDO_URL, {"destino": "BORDADOS"})
        self.assertEqual((r.status_code, list(r.json())), (400, ["destino"]))

    def test_cliente_sucursal_y_q(self):
        self.assertEqual(set(self._ids(estatus="todos", cliente=self.alfa.pk)), {self.pendiente.pk, self.surtido.pk})
        self.assertEqual(self._ids(por_surtir=1, sucursal=self.norte.pk), [self.en_proceso.pk])
        self.assertEqual(self._ids(estatus="todos", q="P988-C-03"), [self.despachado.pk])
        self.assertEqual(set(self._ids(por_surtir=1, q="alfa 988")), {self.pendiente.pk})
        for valor in ("x", "²", "-1"):
            r = self.api.get(SURTIDO_URL, {"cliente": valor})
            self.assertEqual((r.status_code, list(r.json())), (400, ["cliente"]), valor)

    def test_ordering_con_lista_blanca(self):
        folios = [x["folio"] for x in self._lista(estatus="todos", ordering="folio")["results"]]
        self.assertEqual(folios, sorted(folios))
        folios = [x["folio"] for x in self._lista(estatus="todos", ordering="-folio")["results"]]
        self.assertEqual(folios, sorted(folios, reverse=True))
        clientes = [x["cliente_nombre"] for x in self._lista(estatus="todos", ordering="cliente_nombre")["results"]]
        self.assertEqual(clientes, sorted(clientes))
        # Fuera de la lista (una cifra anotada): se ignora, queda ``-id``.
        ids = self._ids(estatus="todos", ordering="pedidas")
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(self._ids(estatus="todos"), sorted(ids, reverse=True))

    # --- filtros/ y resumen/ --------------------------------------------------

    def test_filtros_del_conjunto(self):
        data = self.api.get(f"{SURTIDO_URL}filtros/", {"por_surtir": 1, "cliente": self.beta.pk, "q": "x"}).json()
        self.assertEqual(data["clientes"], [{"id": self.alfa.pk, "nombre": "Alfa 988"}, {"id": self.beta.pk, "nombre": "Beta 988"}])
        self.assertEqual(data["sucursales"], [{"id": self.matriz.pk, "nombre": "Matriz 988"}, {"id": self.norte.pk, "nombre": "Norte 988"}])
        data = self.api.get(f"{SURTIDO_URL}filtros/", {"estatus": "todos", "completo": 1}).json()
        self.assertEqual(data["sucursales"], [{"id": self.matriz.pk, "nombre": "Matriz 988"}])
        self.assertEqual([c["id"] for c in data["clientes"]], [self.alfa.pk, self.beta.pk])

    def test_filtros_nombra_al_cliente_con_la_foto_del_pedido_mas_reciente(self):
        """El nombre vivo del cliente no cuenta: la foto que muestran las filas y busca ``q``."""
        from terceros.models import Cliente
        from ventas.models import Pedido

        Cliente.objects.filter(pk=self.alfa.pk).update(nombre="Alfa renombrado")
        Pedido.objects.filter(pk=self.surtido.pk).update(cliente_nombre="Alfa nueva")  # más reciente que A-01
        clientes = self.api.get(f"{SURTIDO_URL}filtros/", {"estatus": "todos"}).json()["clientes"]
        self.assertEqual([c for c in clientes if c["id"] == self.alfa.pk], [{"id": self.alfa.pk, "nombre": "Alfa nueva"}])
        self.assertEqual(len(clientes), len({c["id"] for c in clientes}))  # una opción por cliente
        clientes = self.api.get(f"{SURTIDO_URL}filtros/", {"por_surtir": 1}).json()["clientes"]
        self.assertIn({"id": self.alfa.pk, "nombre": "Alfa 988"}, clientes)  # sólo A-01 en el conjunto

    def test_banderas_como_las_de_pickings(self):
        for valor in ("True", "si", "SI", "1"):
            self.assertEqual(set(self._ids(por_surtir=valor)), self.POR_SURTIR, valor)
            self.assertEqual(set(self._ids(estatus="todos", completo=valor)), self.COMPLETOS, valor)
        self.assertNotEqual(set(self._ids(por_surtir="no")), self.POR_SURTIR)

    def test_resumen_son_los_counts_de_las_pestanas(self):
        r = self.api.get(f"{SURTIDO_URL}resumen/", {"q": "nada", "cliente": self.alfa.pk, "estatus": "1"})
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertIn("generado_en", data)
        self.assertEqual(
            (data["por_surtir"], data["surtidos"]),
            (self._lista(por_surtir=1)["count"], self._lista(estatus="todos", completo=1)["count"]),
        )
        self.assertEqual((data["por_surtir"], data["surtidos"]), (len(self.POR_SURTIR), len(self.COMPLETOS)))

    # --- Alcance --------------------------------------------------------------

    def test_otra_empresa_no_ve_nada_y_superusuario_usa_su_empresa_activa(self):
        propios = self.POR_SURTIR | self.COMPLETOS
        otra = _api_de(self.otra_admin)
        self.assertFalse(propios & {x["id"] for x in self._lista(api=otra, estatus="todos")["results"]})
        self.assertFalse(propios & {x["id"] for x in self._lista(api=otra, por_surtir=1)["results"]})
        super_otra = Usuario.objects.create(
            username="su988@example.com", email="su988@example.com", empresa=self.otra["empresa"], is_superuser=True,
        )
        api = _api_de(super_otra)
        self.assertFalse(propios & {x["id"] for x in self._lista(api=api, estatus="todos")["results"]})
        self.assertEqual(api.get(f"{SURTIDO_URL}{self.surtido.pk}/").status_code, 404)
        resumen = api.get(f"{SURTIDO_URL}resumen/").json()
        self.assertEqual((resumen["por_surtir"], resumen["surtidos"]), (0, 1))  # el pedido surtido del fixture
        clientes = api.get(f"{SURTIDO_URL}filtros/", {"estatus": "todos"}).json()["clientes"]
        self.assertEqual([c["id"] for c in clientes], [self.otra["cliente"].pk])

    def test_sin_empresa_responde_vacio(self):
        api = _api_de(Usuario.objects.create(username="sin988@example.com", email="sin988@example.com", is_superuser=True))
        self.assertEqual(self._lista(api=api, por_surtir=1)["count"], 0)
        self.assertEqual(self._lista(api=api, estatus="todos", completo=1)["count"], 0)
        r = api.get(f"{SURTIDO_URL}resumen/")
        self.assertEqual((r.status_code, r.json()["por_surtir"], r.json()["surtidos"]), (200, 0, 0))
        self.assertEqual(api.get(f"{SURTIDO_URL}filtros/").json(), {"clientes": [], "sucursales": []})

    def test_usuario_de_una_sucursal(self):
        operador = Usuario.objects.create(
            username="norte988@example.com", email="norte988@example.com", empresa=self.empresa,
            sucursal_default=self.norte,
        )
        api = _api_de(operador)
        self.assertEqual([x["id"] for x in self._lista(api=api, por_surtir=1)["results"]], [self.en_proceso.pk])
        self.assertEqual(api.get(f"{SURTIDO_URL}resumen/").json()["por_surtir"], 1)

    # --- Consultas ------------------------------------------------------------

    def test_consultas_constantes(self):
        casos = (
            ({"estatus": "todos"}, 2),
            ({"estatus": "todos", "completo": 1, "etapa": "SURTIDO,DESPACHADO", "ordering": "folio"}, 2),
            ({"por_surtir": 1, "destino": "APARTADO,BORDADO"}, 4),  # + almacenes y surtido de la página
        )
        for params, esperadas in casos:
            for page_size in (1, 200):
                with self.assertNumQueries(esperadas, msg=f"{params} page_size={page_size}"):
                    self.api.get(SURTIDO_URL, {**params, "page_size": page_size})
        with self.assertNumQueries(2):
            self.api.get(f"{SURTIDO_URL}filtros/", {"por_surtir": 1})
        with self.assertNumQueries(1):
            self.api.get(f"{SURTIDO_URL}resumen/")


class TransferenciaPickingBackfillTests(TestCase):
    """EC-916: ``Transferencia.picking`` de las TR en null (migración ``0021`` y
    ``manage.py ligar_transferencias_picking``, ``transferencia_picking_service``).

    Solo se liga con el texto exacto del código, un picking del mismo folio,
    empresa y sucursal, y evidencia de que es suya; lo dudoso queda en null.
    """

    @classmethod
    def setUpTestData(cls):
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente
        from ventas.models import Pedido

        cls.empresa = Empresa.objects.create(codigo="bkf", razon_social="Backfill SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="B1", nombre="B1")
        cls.sucursal2 = Sucursal.objects.create(empresa=cls.empresa, codigo="B2", nombre="B2")
        cls.usuario = Usuario.objects.create(
            username="bkf", email="bkf@example.com", empresa=cls.empresa, sucursal_default=cls.sucursal,
        )
        cls.a = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="A", nombre="A")
        cls.b = Almacen.objects.create(empresa=cls.empresa, sucursal=cls.sucursal, codigo="B", nombre="B")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            cliente=Cliente.objects.create(empresa=cls.empresa, nombre="Cliente"),
            moneda=Moneda.objects.create(codigo_iso="MXN", nombre="Peso"),
        )

    def setUp(self):
        import importlib
        from datetime import timedelta

        from django.utils import timezone

        self.migracion = importlib.import_module("wms.migrations.0021_transferencia_picking")
        self.t0 = timezone.now() - timedelta(days=1)

    def _picking(self, folio, sucursal=None, segundos=0, transferencia=None):
        from datetime import timedelta

        from wms.models import Picking

        picking = Picking.objects.create(
            folio=folio, empresa=self.empresa, sucursal=sucursal or self.sucursal, pedido=self.pedido,
            operador=self.usuario, usuario=self.usuario, almacen=self.a, transferencia=transferencia,
        )
        Picking.objects.filter(pk=picking.pk).update(created_at=self.t0 + timedelta(seconds=segundos))
        return picking

    def _tr(self, observaciones, segundos=0, sucursal=None):
        from datetime import timedelta

        from wms.models import Transferencia

        tr = Transferencia.objects.create(
            empresa=self.empresa, sucursal=sucursal or self.sucursal, almacen_origen=self.a,
            almacen_destino=self.b, folio=f"TR-{Transferencia.objects.count() + 1}", usuario=self.usuario,
            observaciones=observaciones,
        )
        Transferencia.objects.filter(pk=tr.pk).update(
            fecha_creacion=self.t0 + timedelta(seconds=segundos)
        )
        return tr

    def _auditoria(self, picking, antes=None, despues=None):
        from auditoria.models import AuditoriaEvento

        AuditoriaEvento.objects.create(
            empresa=self.empresa, usuario=self.usuario, modulo="wms", accion="TRASLADO_PICKING",
            tabla="picking", id_registro=str(picking.pk),
            antes_json={"transferencia_id": antes}, despues_json={"transferencia_id": despues},
        )

    def _plan(self):
        from auditoria.models import AuditoriaEvento
        from wms.models import Picking, Transferencia
        from wms.services.transferencia_picking_service import planear

        return planear(Transferencia, Picking, AuditoriaEvento)

    def test_alta_en_la_misma_ventana_se_liga(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=0)
        picking = self._picking("PK-1", segundos=2)
        self.assertEqual(self._plan(), ({tr.pk: picking.pk}, []))

    def test_alta_falsificada_fuera_de_la_ventana_queda_en_null(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=3600)  # escrita después, a mano
        self._picking("PK-1", segundos=0)
        self.assertEqual(self._plan(), ({}, [tr.pk]))

    def test_alta_confirmada_por_picking_transferencia(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=-7200)
        picking = self._picking("PK-1", transferencia=tr)
        self.assertEqual(self._plan(), ({tr.pk: picking.pk}, []))

    def test_traslados_se_ligan_por_la_auditoria(self):
        alta = self._tr("Traspaso del picking PK-1", segundos=0)
        picking = self._picking("PK-1", segundos=1)
        t1 = self._tr("Traslado del picking PK-1", segundos=100)
        t2 = self._tr("Traslado del picking PK-1", segundos=200)
        self._auditoria(picking, antes=alta.pk, despues=t1.pk)
        self._auditoria(picking, antes=t1.pk, despues=t2.pk)
        asignaciones, ambiguas = self._plan()
        self.assertEqual(asignaciones, {alta.pk: picking.pk, t1.pk: picking.pk, t2.pk: picking.pk})
        self.assertEqual(ambiguas, [])

    def test_traslado_sin_auditoria_queda_en_null(self):
        """Todo traslado real deja su auditoría: sin ella, el texto no basta."""
        self._picking("PK-1")
        tr = self._tr("Traslado del picking PK-1", segundos=100)
        self.assertEqual(self._plan(), ({}, [tr.pk]))

    def test_mismo_folio_en_otra_sucursal_no_se_liga(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=0)
        self._picking("PK-1", sucursal=self.sucursal2, segundos=1)
        self.assertEqual(self._plan(), ({}, [tr.pk]))

    def test_mismo_folio_en_dos_sucursales_elige_la_de_la_tr(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=0, sucursal=self.sucursal2)
        self._picking("PK-1", segundos=1)
        propio = self._picking("PK-1", sucursal=self.sucursal2, segundos=1)
        self.assertEqual(self._plan(), ({tr.pk: propio.pk}, []))

    def test_dos_altas_para_un_picking_quedan_en_null(self):
        tr1 = self._tr("Traspaso del picking PK-1", segundos=0)
        tr2 = self._tr("Traspaso del picking PK-1", segundos=1)
        self._picking("PK-1", segundos=2)
        self.assertEqual(self._plan(), ({}, sorted([tr1.pk, tr2.pk])))

    def test_entre_dos_altas_se_queda_la_de_evidencia_estructural(self):
        """La TR real del alta (``Picking.transferencia``) se liga; la que solo cae en
        la ventana de 60 s con el mismo texto se descarta."""
        real = self._tr("Traspaso del picking PK-1", segundos=0)
        falsa = self._tr("Traspaso del picking PK-1", segundos=1)
        picking = self._picking("PK-1", segundos=2, transferencia=real)
        self.assertEqual(self._plan(), ({real.pk: picking.pk}, [falsa.pk]))

    def test_alta_ya_ligada_descarta_la_falsa_en_la_ventana(self):
        """Una TR ya ligada por el código cuenta como evidencia estructural."""
        from wms.models import Transferencia

        real = self._tr("Traspaso del picking PK-1", segundos=0)
        falsa = self._tr("Traspaso del picking PK-1", segundos=1)
        picking = self._picking("PK-1", segundos=2)
        Transferencia.objects.filter(pk=real.pk).update(picking=picking)
        self.assertEqual(self._plan(), ({}, [falsa.pk]))

    def test_comando_es_idempotente_y_liga_tr_nuevas_sin_fk(self):
        """Tras la migración, el build anterior puede crear TR sin FK: el comando las
        liga, y una segunda corrida no cambia nada."""
        from io import StringIO

        from django.core.management import call_command

        from wms.models import Transferencia

        tr = self._tr("Traspaso del picking PK-1", segundos=0)
        picking = self._picking("PK-1", segundos=1)
        salida = StringIO()
        call_command("ligar_transferencias_picking", "--dry-run", stdout=salida)
        self.assertIn("TR ligadas: 1", salida.getvalue())
        self.assertIsNone(Transferencia.objects.get(pk=tr.pk).picking_id)

        call_command("ligar_transferencias_picking", stdout=StringIO())
        self.assertEqual(Transferencia.objects.get(pk=tr.pk).picking_id, picking.pk)
        antes = dict(Transferencia.objects.values_list("pk", "picking_id"))
        salida = StringIO()
        call_command("ligar_transferencias_picking", stdout=salida)
        self.assertIn("TR ligadas: 0", salida.getvalue())
        self.assertEqual(dict(Transferencia.objects.values_list("pk", "picking_id")), antes)

    def test_referencia_a_otro_picking_es_conflicto(self):
        tr = self._tr("Traspaso del picking PK-1", segundos=0)
        self._picking("PK-1", segundos=1)
        self._picking("PK-9", transferencia=tr)  # la FK dice otra cosa que el texto
        self.assertEqual(self._plan(), ({}, [tr.pk]))

    def test_tr_manual_no_se_toca(self):
        manual = self._tr("PRUEBA QA vaciar almacén", segundos=0)
        self._picking("PK-1", segundos=1, transferencia=manual)
        self.assertEqual(self._plan(), ({}, []))

    def test_ligar_transferencias_escribe_y_no_pisa(self):
        from django.apps import apps

        from wms.models import Transferencia

        tr = self._tr("Traspaso del picking PK-1", segundos=0)
        picking = self._picking("PK-1", segundos=1)
        ya_ligada = self._tr("Traspaso del picking PK-2", segundos=0)
        self._picking("PK-2", segundos=1)
        otro = self._picking("PK-3")
        Transferencia.objects.filter(pk=ya_ligada.pk).update(picking=otro)
        dudosa = self._tr("Traslado del picking PK-1", segundos=50)

        self.migracion.ligar_transferencias(apps, None)

        valores = dict(Transferencia.objects.values_list("pk", "picking_id"))
        self.assertEqual(valores[tr.pk], picking.pk)
        self.assertEqual(valores[ya_ligada.pk], otro.pk)
        self.assertIsNone(valores[dudosa.pk])

    def test_picking_es_de_solo_lectura_en_la_api(self):
        from wms.api.serializers import TransferenciaSerializer

        self.assertTrue(TransferenciaSerializer().fields["picking"].read_only)


class PickingListadoServidorTests(TestCase):
    """EC-921: ``?estado=``, ``?prioridad=``, ``?search=``, ``?ordering=`` y ``kpis/`` del
    listado de pickings (modo servidor de ``PickingView``, EC-915).

    Empresa A (superusuario, su empresa activa) con siete pickings de prioridad,
    estado, operador, pedido y líneas distintas; empresa B con uno solo, que no debe
    aparecer nunca.
    """

    URL = "/api/v1/wms/pickings/"
    KPIS = "/api/v1/wms/pickings/kpis/"

    @classmethod
    def setUpTestData(cls):
        from ventas.models import Pedido
        from wms.models import Picking

        cls.a = _documentos_wms("LA", superusuario=True)
        cls.b = _documentos_wms("LB")
        cls.user = cls.a["usuario"]
        # El picking del fixture: MEDIA, Pendiente, 0 líneas, operador sin nombre.
        cls.base = cls.a["picking"]

        def operador(codigo, nombre="", apellido=""):
            return Usuario.objects.create(
                username=f"op-{codigo}@example.com", email=f"op-{codigo}@example.com",
                first_name=nombre, last_name=apellido, empresa=cls.a["empresa"],
            )

        cls.zoe = operador("zoe", "Zoe", "Alvarez")
        cls.ana = operador("ana", "Ana", "Zamora")
        cls.sin_nombre = operador("anonimo")

        def pedido(folio):
            p = cls.a["pedido"]
            return Pedido.objects.create(
                empresa=p.empresa, sucursal=p.sucursal, cliente=p.cliente, moneda=p.moneda, folio=folio,
                estatus=3, persona_pagos="Pagos", correo_facturas="p@example.com", telefono_pagos="8100000000",
                forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            )

        cls.pedido_z = pedido("P-ZZZ")
        cls.pedido_sin_folio = pedido(None)

        def picking(folio, prioridad, estado, total, completas, op, ped=None):
            return Picking.objects.create(
                folio=folio, empresa=cls.a["empresa"], sucursal=cls.a["sucursal"], pedido=ped or cls.a["pedido"],
                operador=op, almacen=cls.base.almacen, usuario=cls.user, prioridad=prioridad, estado=estado,
                total_lineas=total, total_lineas_completas=completas,
            )

        A, M, B = Picking.Prioridad.ALTA, Picking.Prioridad.MEDIA, Picking.Prioridad.BAJA
        E = Picking.Estado
        cls.alta1 = picking("SUR-001", A, E.COMPLETADO, 4, 4, cls.zoe, cls.pedido_z)
        cls.baja1 = picking("SUR-002", B, E.PARCIAL, 5, 2, cls.ana)
        cls.alta2 = picking("SUR-003", A, E.PARCIAL, 3, 1, cls.ana, cls.pedido_sin_folio)
        cls.media1 = picking("SUR-004", M, E.PENDIENTE, 6, 0, cls.zoe)
        cls.baja2 = picking("SUR-005", B, E.COMPLETADO, 2, 2, cls.sin_nombre)
        # Dato inconsistente: más líneas completas que totales; no debe restar.
        cls.raro = picking("SUR-006", M, E.CANCELADO, 1, 3, cls.sin_nombre)

    def setUp(self):
        self.api = _api_de(self.user)

    def _get(self, url, **params):
        resp = self.api.get(url, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _ids(self, **params):
        return [fila["id"] for fila in self._get(self.URL, **params)]

    @staticmethod
    def _pks(*pickings):
        return [p.pk for p in pickings]

    def _por_defecto(self):
        from wms.models import Picking

        return list(
            Picking.objects.filter(empresa=self.a["empresa"]).order_by("-created_at", "-id").values_list("id", flat=True)
        )

    # --- Sin parámetros ----------------------------------------------------------

    def test_sin_parametros_responde_la_lista_completa_con_renglones(self):
        datos = self._get(self.URL)
        self.assertIsInstance(datos, list)
        self.assertEqual([f["id"] for f in datos], self._por_defecto())
        self.assertIn("picking_detalle", datos[0])
        self.assertIn("ordenes_trabajo", datos[0])

    def test_el_detalle_ignora_los_parametros_del_listado(self):
        url = f"{self.URL}{self.alta1.pk}/"
        limpio = self._get(url)
        con_params = self._get(url, search="no-coincide", estado="Pendiente", prioridad="BAJA", ordering="prioridad")
        self.assertEqual(con_params, limpio)
        self.assertEqual(self.api.get(url, {"estado": "Inexistente"}).status_code, 200)

    # --- Filtros -----------------------------------------------------------------

    def test_filtra_por_estado_uno_o_varios(self):
        self.assertCountEqual(self._ids(estado="Completado"), self._pks(self.alta1, self.baja2))
        self.assertCountEqual(
            self._ids(estado="Completado,Parcial"), self._pks(self.alta1, self.baja2, self.baja1, self.alta2)
        )
        self.assertEqual(self._ids(estado="En proceso"), [])

    def test_filtra_por_prioridad_uno_o_varios(self):
        self.assertCountEqual(self._ids(prioridad="ALTA"), self._pks(self.alta1, self.alta2))
        self.assertCountEqual(
            self._ids(prioridad="BAJA,MEDIA"), self._pks(self.baja1, self.baja2, self.media1, self.raro, self.base)
        )

    def test_estado_o_prioridad_inexistente_es_400(self):
        for params, campo in [
            ({"estado": "Terminado"}, "estado"),
            ({"estado": "Completado,completado"}, "estado"),
            ({"prioridad": "alta"}, "prioridad"),
            ({"prioridad": "URGENTE"}, "prioridad"),
            ({"prioridad": ","}, "prioridad"),
        ]:
            for url in (self.URL, self.KPIS):
                with self.subTest(url=url, **params):
                    resp = self.api.get(url, params)
                    self.assertEqual(resp.status_code, 400, resp.content)
                    self.assertIn(campo, resp.json())

    # --- Búsqueda ----------------------------------------------------------------

    def test_busca_por_folio_pedido_y_operador(self):
        casos = {
            "sur-003": [self.alta2],
            "P-ZZZ": [self.alta1],
            "zoe": [self.alta1, self.media1],
            "Ana Zamora": [self.baja1, self.alta2],
            # Sin nombre, la fila pinta el correo: se busca por él.
            "op-anonimo": [self.baja2, self.raro],
            # Con nombre, el correo no se pinta ni se busca.
            "op-zoe": [],
            "example.com": [self.base, self.baja2, self.raro],
        }
        for termino, esperados in casos.items():
            with self.subTest(search=termino):
                self.assertCountEqual(self._ids(search=termino), self._pks(*esperados))

    def test_la_busqueda_no_alcanza_otra_empresa(self):
        self.assertEqual(self._ids(search="PICK-LB"), [])
        self.assertEqual(self._ids(search="P-LB"), [])

    def test_busqueda_y_filtros_se_combinan(self):
        self.assertEqual(self._ids(search="ana", prioridad="ALTA"), self._pks(self.alta2))

    # --- Ordenamiento ------------------------------------------------------------

    def test_prioridad_ordena_por_urgencia_con_desempate_por_id(self):
        baja = sorted(self._pks(self.baja1, self.baja2))
        media = sorted(self._pks(self.base, self.media1, self.raro))
        alta = sorted(self._pks(self.alta1, self.alta2))
        self.assertEqual(self._ids(ordering="prioridad"), baja + media + alta)
        self.assertEqual(self._ids(ordering="-prioridad"), alta[::-1] + media[::-1] + baja[::-1])

    def test_operador_nombre_ordena_por_el_nombre_que_se_muestra(self):
        """El mismo texto que ``operador_nombre`` (nombre completo o, sin él, el correo),
        con el desempate por ``id``. El orden entre textos es el de la collation de la
        base, igual que cualquier otra columna de texto."""
        filas = self._get(self.URL, ordering="operador_nombre", resumen=1)
        claves = [(f["operador_nombre"], f["id"]) for f in filas]
        self.assertEqual(len({nombre for nombre, _ in claves}), 4)
        # Agrupados por nombre y, dentro de cada nombre, por id ascendente.
        for nombre in {n for n, _ in claves}:
            ids = [pk for n, pk in claves if n == nombre]
            self.assertEqual(ids, sorted(ids))
            posiciones = [i for i, (n, _) in enumerate(claves) if n == nombre]
            self.assertEqual(posiciones, list(range(posiciones[0], posiciones[0] + len(posiciones))))
        desc = self._get(self.URL, ordering="-operador_nombre", resumen=1)
        self.assertEqual([f["operador_nombre"] for f in desc], [n for n, _ in claves][::-1])
        self.assertEqual([f["id"] for f in desc], [pk for _, pk in claves][::-1])

    def test_pedido_folio_ordena_con_los_sin_folio_al_final(self):
        asc = self._ids(ordering="pedido_folio")
        desc = self._ids(ordering="-pedido_folio")
        self.assertEqual(asc[-1], self.alta2.pk)
        self.assertEqual(desc[-1], self.alta2.pk)
        self.assertEqual(desc[0], self.alta1.pk)  # P-ZZZ

    def test_columnas_simples_y_campo_fuera_de_la_lista(self):
        self.assertEqual(
            self._ids(ordering="folio"),
            self._pks(self.base, self.alta1, self.baja1, self.alta2, self.media1, self.baja2, self.raro),
        )
        self.assertEqual(self._ids(ordering="-total_lineas")[0], self.media1.pk)
        self.assertEqual(self._ids(ordering="estado,folio")[:2], self._pks(self.raro, self.alta1))
        for invalido in ("observaciones", "almacen_nombre", "operador__password"):
            with self.subTest(ordering=invalido):
                self.assertEqual(self._ids(ordering=invalido), self._por_defecto())

    # --- Paginación y resumen ----------------------------------------------------

    def test_filtros_busqueda_y_orden_con_resumen_y_pagina(self):
        params = {"prioridad": "ALTA,BAJA", "ordering": "-prioridad", "resumen": 1, "limit": 2, "offset": 1}
        datos = self._get(self.URL, **params)
        completos = self._ids(prioridad="ALTA,BAJA", ordering="-prioridad")
        self.assertEqual(datos["count"], 4)
        self.assertEqual([f["id"] for f in datos["results"]], completos[1:3])
        self.assertNotIn("picking_detalle", datos["results"][0])
        self.assertNotIn("ordenes_trabajo", datos["results"][0])
        self.assertEqual(self._get(self.URL, search="zoe", resumen=1, limit=1)["count"], 2)

    # --- KPIs --------------------------------------------------------------------

    @staticmethod
    def _kpis_del_front(filas):
        """``computePickingKpis`` del front sobre las filas del listado."""
        por_prioridad = {"ALTA": 0, "MEDIA": 0, "BAJA": 0}
        lineas = 0
        for f in filas:
            lineas += max(0, f["total_lineas"] - f["total_lineas_completas"])
            if f["prioridad"] in por_prioridad:
                por_prioridad[f["prioridad"]] += 1
        return {"total": len(filas), "lineas_por_surtir": lineas, "por_prioridad": por_prioridad}

    @staticmethod
    def _plano(kpis):
        return {
            "total": kpis["total"],
            "lineas_por_surtir": kpis["lineas_por_surtir"],
            "por_prioridad": {b["prioridad"]: b["total"] for b in kpis["por_prioridad"]},
        }

    def test_kpis_igual_que_el_front_sobre_el_listado_filtrado(self):
        for params in ({}, {"estado": "Parcial,Completado"}, {"prioridad": "MEDIA"}, {"search": "ana"},
                       {"search": "sin-coincidencias"}, {"search": "zoe", "estado": "Pendiente"}):
            with self.subTest(**params):
                kpis = self._get(self.KPIS, **params)
                self.assertEqual(self._plano(kpis), self._kpis_del_front(self._get(self.URL, **params)))
                self.assertEqual(kpis["total"], self._get(self.URL, limit=1, **params)["count"])

    def test_kpis_sin_filtros(self):
        kpis = self._get(self.KPIS)
        self.assertIn("generado_en", kpis)
        self.assertEqual(kpis["total"], 7)
        # 0 (fixture) + 0 + 3 + 2 + 6 + 0 + 0: el inconsistente (1 de 3) no resta.
        self.assertEqual(kpis["lineas_por_surtir"], 11)
        self.assertEqual(kpis["por_prioridad"], [
            {"prioridad": "ALTA", "prioridad_label": "Alta", "total": 2},
            {"prioridad": "MEDIA", "prioridad_label": "Media", "total": 3},
            {"prioridad": "BAJA", "prioridad_label": "Baja", "total": 2},
        ])

    def test_kpis_prioridad_fuera_del_catalogo_cuenta_solo_en_total(self):
        from wms.models import Picking

        Picking.objects.filter(pk=self.raro.pk).update(prioridad="URGENTE")
        kpis = self._plano(self._get(self.KPIS))
        self.assertEqual(kpis, self._kpis_del_front(self._get(self.URL)))
        self.assertEqual(kpis["total"], 7)
        self.assertEqual(sum(kpis["por_prioridad"].values()), 6)

    def test_kpis_ignoran_pagina_y_orden(self):
        self.assertEqual(
            self._plano(self._get(self.KPIS, prioridad="ALTA,BAJA", limit=1, offset=1, ordering="-operador_nombre")),
            self._plano(self._get(self.KPIS, prioridad="ALTA,BAJA")),
        )

    def test_kpis_en_una_consulta(self):
        with self.assertNumQueries(1):
            resp = self.api.get(self.KPIS, {"search": "zoe", "estado": "Completado,Pendiente", "ordering": "-prioridad"})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_kpis_y_listado_acotados_a_la_empresa_activa(self):
        self.assertEqual(self._get(self.KPIS, search="LB")["total"], 0)
        admin_b = _api_de(self.b["usuario"])
        self.assertEqual(admin_b.get(self.KPIS).json()["total"], 1)
        self.assertEqual(admin_b.get(self.KPIS, {"search": "SUR"}).json()["total"], 0)
        self.assertEqual(admin_b.get(self.URL, {"search": "SUR"}).json(), [])

    def test_superusuario_sigue_a_su_empresa_activa(self):
        Usuario.objects.filter(pk=self.user.pk).update(empresa=self.b["empresa"])
        api = _api_de(Usuario.objects.get(pk=self.user.pk))
        self.assertEqual(api.get(self.KPIS).json()["total"], 1)
        self.assertEqual([f["id"] for f in api.get(self.URL, {"prioridad": "MEDIA"}).json()], [self.b["picking"].pk])

    def test_sin_empresa_kpis_en_cero_y_listado_vacio(self):
        Usuario.objects.filter(pk=self.user.pk).update(empresa=None)
        api = _api_de(Usuario.objects.get(pk=self.user.pk))
        self.assertEqual(api.get(self.URL, {"prioridad": "ALTA"}).json(), [])
        kpis = api.get(self.KPIS).json()
        self.assertEqual((kpis["total"], kpis["lineas_por_surtir"]), (0, 0))
        self.assertEqual([b["total"] for b in kpis["por_prioridad"]], [0, 0, 0])

    def test_kpis_requieren_sesion(self):
        self.assertIn(APIClient().get(self.KPIS).status_code, (401, 403))

    # --- Usuario sin admin: sólo sus sucursales -----------------------------------

    def _otra_sucursal_con_picking(self):
        """Una segunda sucursal de la empresa A con un picking ALTA que, por folio,
        pedido y operador, coincidiría con los filtros y la búsqueda de las pruebas."""
        from wms.models import Picking

        otra = Sucursal.objects.create(empresa=self.a["empresa"], codigo="SLA2", nombre="Sucursal LA2")
        return Picking.objects.create(
            folio="SUR-900", empresa=self.a["empresa"], sucursal=otra, pedido=self.pedido_z, operador=self.zoe,
            almacen=self.base.almacen, usuario=self.user, prioridad=Picking.Prioridad.ALTA,
            estado=Picking.Estado.PARCIAL, total_lineas=50, total_lineas_completas=0,
        )

    def _usuario_de_sucursal(self):
        return Usuario.objects.create(
            username="wms-sucursal-la@example.com", email="wms-sucursal-la@example.com",
            empresa=self.a["empresa"], sucursal_default=self.a["sucursal"],
        )

    def test_usuario_de_sucursal_no_ve_otras_sucursales_ni_empresas(self):
        ajeno = self._otra_sucursal_con_picking()
        api = _api_de(self._usuario_de_sucursal())
        propios = [self.base, self.alta1, self.baja1, self.alta2, self.media1, self.baja2, self.raro]
        for params, esperados in [
            ({}, propios),
            ({"prioridad": "ALTA"}, [self.alta1, self.alta2]),
            ({"estado": "Parcial"}, [self.baja1, self.alta2]),
            ({"search": "zoe"}, [self.alta1, self.media1]),
            ({"search": "SUR-900"}, []),
            ({"search": "PICK-LB"}, []),
            ({"search": "P-ZZZ", "prioridad": "ALTA", "estado": "Parcial,Completado"}, [self.alta1]),
        ]:
            with self.subTest(**params):
                filas = api.get(self.URL, params).json()
                ids = [f["id"] for f in filas]
                self.assertCountEqual(ids, self._pks(*esperados))
                self.assertNotIn(ajeno.pk, ids)
                pagina = api.get(self.URL, {**params, "limit": 2, "resumen": 1}).json()
                self.assertEqual(pagina["count"], len(esperados))
                kpis = api.get(self.KPIS, params).json()
                self.assertEqual(self._plano(kpis), self._kpis_del_front(filas))

    def test_kpis_de_usuario_de_sucursal_sin_totales_de_otra_sucursal(self):
        self._otra_sucursal_con_picking()
        kpis = _api_de(self._usuario_de_sucursal()).get(self.KPIS, {"prioridad": "ALTA"}).json()
        # Sólo SUR-001 (0 pendientes) y SUR-003 (2): el ALTA de la otra sucursal (50) no cuenta.
        self.assertEqual((kpis["total"], kpis["lineas_por_surtir"]), (2, 2))
        # El superusuario sí lo ve: es de su empresa activa.
        self.assertEqual(self._get(self.KPIS, prioridad="ALTA")["total"], 3)

    def test_usuario_de_sucursal_kpis_en_una_consulta(self):
        api = _api_de(self._usuario_de_sucursal())
        with self.assertNumQueries(1):
            resp = api.get(self.KPIS, {"search": "zoe", "estado": "Completado,Pendiente"})
        self.assertEqual(resp.status_code, 200, resp.content)

    # --- trasladar ignora los parámetros del listado ------------------------------

    def test_trasladar_ignora_busqueda_y_filtros_del_listado(self):
        """``trasladar`` resuelve el picking con ``get_object()`` (filter backends
        incluidos): un ``?search=`` o un filtro que no coincide no lo vuelven 404. El
        mismo almacén responde el 400 de negocio sin mover nada."""
        url = f"{self.URL}{self.alta1.pk}/trasladar/"
        cuerpo = {"almacen": self.alta1.almacen_id}
        limpio = self.api.post(url, cuerpo, format="json")
        self.assertEqual(limpio.status_code, 400, limpio.content)
        self.assertIn("almacen", limpio.json())
        for params in ("search=no-coincide", "estado=Pendiente&prioridad=BAJA", "estado=Inexistente",
                       "search=x&ordering=-prioridad"):
            with self.subTest(params=params):
                resp = self.api.post(f"{url}?{params}", cuerpo, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), limpio.json())


class CancelacionesEC949Tests(TestCase):
    """EC-949: quién canceló (``cancelado_por_nombre``), enlace original ↔ reversa de
    TR y la cadena de TR de un picking en ``revertir/`` (sólo la última, y el picking
    queda en el almacén donde quedan las prendas)."""

    URL_TR = "/api/v1/wms/transferencias/"
    URL_PICKINGS = "/api/v1/wms/pickings/"
    URL_PACKINGS = "/api/v1/wms/packings/"
    URL_DESPACHOS = "/api/v1/wms/despachos/"

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Talla
        from inventarios.models import Almacen, Existencia
        from nucleo.models import Moneda, SerieFolio
        from terceros.models import Cliente
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

        cls.empresa = Empresa.objects.create(codigo="ec949", razon_social="EC949 SA")
        cls.otra_empresa = Empresa.objects.create(codigo="ec949-x", razon_social="Otra SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.admin = Usuario.objects.create(
            username="ec949-admin", email="ec949-admin@example.com", first_name="Ana", last_name="López",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        # Sin nombre: ``cancelado_por_nombre`` cae al correo.
        cls.sin_nombre = Usuario.objects.create(
            username="ec949-sn", email="ec949-sn@example.com",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.intruso = Usuario.objects.create(
            username="ec949-x", email="ec949-x@example.com", empresa=cls.otra_empresa, is_admin_empresa=True,
        )
        for tipo, prefijo in (("Picking", "PK"), ("Transferencia", "TR")):
            SerieFolio.objects.create(
                empresa=cls.empresa, sucursal=cls.sucursal, tipo_documento=tipo, serie="A", prefijo=prefijo,
            )
        kw = dict(empresa=cls.empresa, sucursal=cls.sucursal, permite_salida=True, permite_entrada=True)
        cls.terminado = Almacen.objects.create(codigo="PT", nombre="Terminado", **kw)
        cls.bordados = Almacen.objects.create(codigo="BOR", nombre="Bordados", **kw)
        cls.apartados = Almacen.objects.create(codigo="APA", nombre="Apartados", **kw)
        cls.producto = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera", codigo="EC949"
        )
        # Bordados ya tiene piezas del mismo SKU que no son del picking: sin la regla de
        # la cadena, revertir el alta se las llevaría en vez de fallar por inventario.
        Existencia.objects.create(producto=cls.producto, almacen=cls.terminado, stock=10, cantidad=Decimal("10"))
        Existencia.objects.create(producto=cls.producto, almacen=cls.bordados, stock=10, cantidad=Decimal("10"))
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cliente = Cliente.objects.create(empresa=cls.empresa, nombre="Cliente")
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, cliente=cliente, moneda=moneda, folio="P-949",
            estatus=3, programacion_conf={"programaciones": [{"destino": "APARTADO", "cantidad": 4}]},
            persona_pagos="Pagos", correo_facturas="ec949@example.com", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
        )
        detalle = PedidoDetalle.objects.create(pedido=cls.pedido, producto=cls.producto)
        cls.talla_row = PedidoDetalleTalla.objects.create(
            pedido_detalle=detalle, talla=Talla.objects.create(nombre="EC949-M"), cantidad=4
        )

    def _api(self, user=None):
        api = APIClient()
        api.force_authenticate(user or self.admin)
        return api

    def _filas(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        datos = resp.json()
        return datos.get("results", datos) if isinstance(datos, dict) else datos

    def _existencias(self):
        from inventarios.models import Existencia
        return {
            almacen.pk: sum(
                (e.cantidad for e in Existencia.objects.filter(producto=self.producto, almacen=almacen)),
                Decimal("0"),
            )
            for almacen in (self.terminado, self.bordados, self.apartados)
        }

    def _alta(self):
        """Picking real: el alta crea la TR terminado → bordados (4 piezas)."""
        from wms.models import Picking
        resp = self._api().post(self.URL_PICKINGS, {
            "pedido": self.pedido.pk, "operador": self.admin.pk,
            "almacen": self.terminado.pk, "almacen_destino": self.bordados.pk,
            "picking_detalle": [{"pedido_detalle_talla": self.talla_row.pk, "cantidad_asignada": "4"}],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Picking.objects.get(pk=resp.json()["id"])

    def _trasladar(self, picking, almacen):
        resp = self._api().post(
            f"{self.URL_PICKINGS}{picking.pk}/trasladar/", {"almacen": almacen.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def _cancelar(self, url, doc_id, user=None):
        return self._api(user).post(f"{url}{doc_id}/cancelar/", {"motivo": "Prueba EC-949"}, format="json")

    def _revertir(self, tr, user=None):
        return self._api(user).post(f"{self.URL_TR}{tr.pk}/revertir/", {"motivo": "Prueba EC-949"}, format="json")

    def _almacen_actual(self, picking):
        """``(almacen_actual del detalle, almacenes cuyo ?almacen_actual= lo lista)``."""
        detalle = self._api().get(f"{self.URL_PICKINGS}{picking.pk}/").json()
        en_filtro = {
            almacen.pk for almacen in (self.terminado, self.bordados, self.apartados)
            if picking.pk in {f["id"] for f in self._filas(
                self._api().get(f"{self.URL_PICKINGS}?resumen=1&almacen_actual={almacen.pk}")
            )}
        }
        return detalle["almacen_actual"], en_filtro

    def _tr_manual(self, origen, destino, cantidad="1"):
        from wms.services.transferencia_service import TransferenciaService
        return TransferenciaService.handle_store(
            {"almacen_origen": origen, "almacen_destino": destino, "observaciones": "Manual EC-949",
             "transferencia_detalle": [{"producto": self.producto, "cantidad": Decimal(cantidad)}]},
            self.admin,
        )

    def _picking_orm(self, folio, cancelado_por=None):
        from django.utils import timezone

        from wms.models import Picking
        cancelacion = {
            "estado": Picking.Estado.CANCELADO, "cancelado_por": cancelado_por,
            "fecha_cancelacion": timezone.now(), "motivo_cancelacion": "x",
        } if cancelado_por else {}
        return Picking.objects.create(
            folio=folio, empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido, operador=self.admin,
            almacen=self.terminado, usuario=self.admin, **cancelacion,
        )

    def _packing_orm(self, folio, picking, cancelado_por=None):
        from wms.models import Estado, Packing
        cancelacion = {"estado": Estado.CANCELADO, "cancelado_por": cancelado_por} if cancelado_por else {}
        return Packing.objects.create(
            folio=folio, empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido, picking=picking,
            operador=self.admin, usuario=self.admin, **cancelacion,
        )

    def _despacho_orm(self, packing, cancelado_por=None):
        from wms.models import Despacho
        cancelacion = {"estado": Despacho.Estado.CANCELADO, "cancelado_por": cancelado_por} if cancelado_por else {}
        return Despacho.objects.create(packing=packing, usuario=self.admin, **cancelacion)

    # --- cancelado_por / cancelado_por_nombre ------------------------------------

    def test_cancelado_por_nombre_en_listado_y_detalle_de_los_cuatro(self):
        picking = self._picking_orm("PK-949-A")
        vivo = self._picking_orm("PK-949-B")
        packing = self._packing_orm("PACK-949-A", picking)
        packing_vivo = self._packing_orm("PACK-949-B", vivo)
        despacho = self._despacho_orm(packing)
        despacho_vivo = self._despacho_orm(packing_vivo)
        # La viva primero: una TR posterior al mismo destino bloquearía la reversa
        # (movimientos ajenos, ``_movimientos_ajenos_posteriores``).
        tr_viva = self._tr_manual(self.terminado, self.bordados)
        tr = self._tr_manual(self.terminado, self.bordados)

        # De atrás hacia adelante, por la API: un admin con nombre y otro sin nombre.
        self.assertEqual(self._cancelar(self.URL_DESPACHOS, despacho.pk).status_code, 200)
        self.assertEqual(self._cancelar(self.URL_PACKINGS, packing.pk, self.sin_nombre).status_code, 200)
        resp = self._cancelar(self.URL_PICKINGS, picking.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["cancelado_por_nombre"], "Ana López")
        resp = self._revertir(tr, self.sin_nombre)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["original"]["cancelado_por_nombre"], "ec949-sn@example.com")

        casos = (
            (self.URL_DESPACHOS, despacho.pk, despacho_vivo.pk, self.admin, "Ana López", ""),
            (self.URL_PACKINGS, packing.pk, packing_vivo.pk, self.sin_nombre, "ec949-sn@example.com", ""),
            (self.URL_PICKINGS, picking.pk, vivo.pk, self.admin, "Ana López", ""),
            (self.URL_PICKINGS, picking.pk, vivo.pk, self.admin, "Ana López", "?resumen=1"),
            (self.URL_TR, tr.pk, tr_viva.pk, self.sin_nombre, "ec949-sn@example.com", ""),
        )
        for url, cancelado, vivo_id, usuario, nombre, params in casos:
            with self.subTest(url=url, params=params):
                filas = {f["id"]: f for f in self._filas(self._api().get(f"{url}{params}"))}
                detalle = self._api().get(f"{url}{cancelado}/").json()
                for fila in (filas[cancelado], detalle):
                    self.assertEqual((fila["cancelado_por"], fila["cancelado_por_nombre"]), (usuario.pk, nombre))
                self.assertEqual(
                    (filas[vivo_id]["cancelado_por"], filas[vivo_id]["cancelado_por_nombre"]), (None, None)
                )

    def test_listados_no_suman_consultas_por_documento_cancelado(self):
        """Cada documento lo canceló otro usuario: si ``cancelado_por``/``reversa``/
        ``reversa_de`` no viajaran en el ``select_related``, cada fila sumaría consultas."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from wms.models import Transferencia

        def sembrar(n):
            quien = Usuario.objects.create(
                username=f"ec949-u{n}", email=f"ec949-u{n}@example.com", first_name=f"U{n}",
                empresa=self.empresa, sucursal_default=self.sucursal,
            )
            picking = self._picking_orm(f"PK-949-Q{n}", cancelado_por=quien)
            packing = self._packing_orm(f"PACK-949-Q{n}", picking, cancelado_por=quien)
            self._despacho_orm(packing, cancelado_por=quien)
            original = self._tr_manual(self.terminado, self.bordados)
            reversa = self._tr_manual(self.bordados, self.terminado)
            Transferencia.objects.filter(pk=original.pk).update(
                status=Transferencia.TransferenciaStatus.CANCELADA, cancelado_por=quien
            )
            Transferencia.objects.filter(pk=reversa.pk).update(reversa_de=original)

        urls = (self.URL_TR, self.URL_PICKINGS, f"{self.URL_PICKINGS}?resumen=1", self.URL_PACKINGS,
                self.URL_DESPACHOS)

        def contar():
            cuentas = {}
            for url in urls:
                with CaptureQueriesContext(connection) as consultas:
                    self._filas(self._api().get(url))
                cuentas[url] = len(consultas)
            return cuentas

        sembrar(1)
        pocas = contar()
        for n in range(2, 5):
            sembrar(n)
        muchas = contar()
        self.assertEqual(muchas, pocas)

    # --- enlace original ↔ reversa -------------------------------------------------

    def test_reversa_y_reversa_de_con_folio(self):
        # La intacta primero: una TR posterior al mismo destino bloquearía la reversa.
        intacta = self._tr_manual(self.terminado, self.bordados)
        original = self._tr_manual(self.terminado, self.bordados)
        resp = self._revertir(original)
        self.assertEqual(resp.status_code, 201, resp.content)
        reversa_id, reversa_folio = resp.json()["reversa"]["id"], resp.json()["reversa"]["folio"]
        self.assertEqual(
            (resp.json()["original"]["reversa"], resp.json()["original"]["reversa_folio"]),
            (reversa_id, reversa_folio),
        )
        self.assertEqual(
            (resp.json()["reversa"]["reversa_de"], resp.json()["reversa"]["reversa_de_folio"]),
            (original.pk, original.folio),
        )
        filas = {f["id"]: f for f in self._filas(self._api().get(self.URL_TR))}
        campos = ("reversa", "reversa_folio", "reversa_de", "reversa_de_folio")
        esperado = {
            original.pk: (reversa_id, reversa_folio, None, None),
            reversa_id: (None, None, original.pk, original.folio),
            intacta.pk: (None, None, None, None),
        }
        for tr_id, valores in esperado.items():
            with self.subTest(tr=tr_id):
                detalle = self._api().get(f"{self.URL_TR}{tr_id}/").json()
                for fila in (filas[tr_id], detalle):
                    self.assertEqual(tuple(fila[c] for c in campos), valores)

    # --- cadena de TR del picking y almacen_actual ---------------------------------

    def test_cadena_solo_revierte_la_ultima_y_el_picking_queda_donde_quedan_las_prendas(self):
        from auditoria.models import AuditoriaEvento
        from wms.models import Transferencia

        picking = self._alta()
        alta = picking.transferencia
        self.assertEqual(self._almacen_actual(picking), (self.bordados.pk, {self.bordados.pk}))
        self._trasladar(picking, self.apartados)
        picking.refresh_from_db()
        traslado = picking.transferencia
        self.assertEqual((alta.picking_id, traslado.picking_id), (picking.pk, picking.pk))
        self.assertEqual(self._almacen_actual(picking), (self.apartados.pk, {self.apartados.pk}))
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)

        # La TR vieja no se revierte: hay un traslado posterior vivo. Bordados tiene
        # piezas ajenas del mismo SKU, así que el 400 es de la regla, no de inventario.
        antes = self._existencias()
        resp = self._revertir(alta)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"transferencia": (
            f"Sólo se revierte la última TR del picking {picking.folio}. Revierte primero {traslado.folio}."
        )})
        self.assertEqual(self._existencias(), antes)
        self.assertEqual(Transferencia.objects.get(pk=alta.pk).status, Transferencia.TransferenciaStatus.COMPLETADA)

        # El traslado sí: las prendas vuelven a bordados y el picking con ellas.
        self.assertEqual(self._revertir(traslado).status_code, 201)
        self.assertEqual(self._almacen_actual(picking), (self.bordados.pk, {self.bordados.pk}))
        evento = AuditoriaEvento.objects.get(tabla="transferencias", id_registro=str(traslado.pk), accion="CANCELAR")
        self.assertEqual(
            (evento.antes_json["picking_almacen_id"], evento.despues_json["picking_almacen_id"]),
            (self.apartados.pk, self.bordados.pk),
        )

        # Ya es la última viva (la reversa no cuenta): ahora sí el alta.
        self.assertEqual(self._revertir(alta).status_code, 201)
        self.assertEqual(self._almacen_actual(picking), (self.terminado.pk, {self.terminado.pk}))
        picking.refresh_from_db()
        # ``almacen_destino`` y ``transferencia`` se conservan.
        self.assertEqual((picking.almacen_destino_id, picking.transferencia_id), (None, traslado.pk))
        self.assertEqual(self._existencias(), {
            self.terminado.pk: Decimal("10"), self.bordados.pk: Decimal("10"), self.apartados.pk: Decimal("0"),
        })

    def test_alta_sin_traslado_revierte_y_vuelve_al_origen(self):
        picking = self._alta()
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)
        self.assertEqual(self._revertir(picking.transferencia).status_code, 201)
        self.assertEqual(self._almacen_actual(picking), (self.terminado.pk, {self.terminado.pk}))
        picking.refresh_from_db()
        self.assertEqual(picking.almacen_destino_id, self.bordados.pk)

    def test_tr_sin_fk_al_picking_se_resuelve_por_picking_transferencia(self):
        """TR sin ``Transferencia.picking`` (anteriores a EC-916): el picking sale de
        ``Picking.transferencia`` y aplican sus reglas (vivo → 400; cancelado → revierte)."""
        from wms.models import Transferencia

        picking = self._alta()
        alta = picking.transferencia
        Transferencia.objects.filter(pk=alta.pk).update(picking=None)
        resp = self._revertir(alta)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("picking vivo", resp.json()["transferencia"])
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)
        self.assertEqual(self._revertir(alta).status_code, 201)
        self.assertEqual(self._almacen_actual(picking), (self.terminado.pk, {self.terminado.pk}))

    def _cancelado_con_traslado(self):
        """``(picking cancelado, TR del alta, TR del traslado)``."""
        picking = self._alta()
        alta = picking.transferencia
        self._trasladar(picking, self.apartados)
        picking.refresh_from_db()
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)
        return picking, alta, picking.transferencia

    def test_cadena_cuenta_el_traslado_sin_fk_al_picking(self):
        """El traslado sin ``Transferencia.picking`` sigue siendo la última TR (es la
        ``Picking.transferencia``): el alta no se revierte antes."""
        from wms.models import Transferencia

        picking, alta, traslado = self._cancelado_con_traslado()
        Transferencia.objects.filter(pk=traslado.pk).update(picking=None)
        resp = self._revertir(alta)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"transferencia": (
            f"Sólo se revierte la última TR del picking {picking.folio}. Revierte primero {traslado.folio}."
        )})

    def _movimiento_en_apartados(self, tipo, cantidad, despacho=None):
        from inventarios.models import MovimientoInventario, MovimientoInventarioDetalle
        mov = MovimientoInventario.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido, despacho=despacho,
            tipo_movimiento=tipo, usuario=self.admin,
        )
        MovimientoInventarioDetalle.objects.create(
            movimiento_inventario=mov, producto=self.producto, almacen=self.apartados, cantidad=Decimal(cantidad),
        )
        return mov

    def test_despacho_cancelado_del_picking_no_bloquea_la_reversa(self):
        """#1362: la salida del despacho del propio picking y el reintegro de su
        cancelación se compensan; no son movimientos ajenos y la reversa del traslado
        procede (antes, 400 tras cancelar despacho, packing y picking)."""
        from inventarios.models import TipoMovimiento
        from wms.models import Despacho

        picking = self._alta()
        self._trasladar(picking, self.apartados)
        picking.refresh_from_db()
        traslado = picking.transferencia
        packing = self._packing_orm("PACK-949-1362", picking)
        despacho = self._despacho_orm(packing)
        # Lo que dejan el alta del despacho (SALIDA) y su cancelación (ENTRADA).
        self._movimiento_en_apartados(TipoMovimiento.SALIDA, "4", despacho)
        self._movimiento_en_apartados(TipoMovimiento.ENTRADA, "4", despacho)
        Despacho.objects.filter(pk=despacho.pk).update(estado=Despacho.Estado.CANCELADO)
        self.assertEqual(self._cancelar(self.URL_PACKINGS, packing.pk).status_code, 200)
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)

        resp = self._revertir(traslado)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self._existencias(), {
            self.terminado.pk: Decimal("6"), self.bordados.pk: Decimal("14"), self.apartados.pk: Decimal("0"),
        })

    def test_salida_manual_posterior_si_bloquea_la_reversa(self):
        """#1362: lo que no es de la cadena del picking —aquí, una salida manual del
        mismo SKU en el destino— sí bloquea la reversa."""
        from inventarios.models import TipoMovimiento

        _picking, _alta, traslado = self._cancelado_con_traslado()
        mov = self._movimiento_en_apartados(TipoMovimiento.SALIDA, "1")
        resp = self._revertir(traslado)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn(f"el movimiento salida #{mov.pk}", resp.json()["transferencia"])

    def test_tr_ligada_entre_la_lectura_y_el_lock_responde_400(self):
        from wms.models import Transferencia
        from wms.services import cancelacion_service

        picking, _alta, traslado = self._cancelado_con_traslado()
        # Fuera del lock resuelve el picking; bajo lock, otro (otro proceso la cambió).
        with patch.object(cancelacion_service, "_picking_id_de", side_effect=[picking.pk, None]):
            resp = self._revertir(traslado)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"transferencia": "La TR cambió mientras se revertía. Intenta de nuevo."})
        self.assertEqual(
            Transferencia.objects.get(pk=traslado.pk).status, Transferencia.TransferenciaStatus.COMPLETADA
        )

    def test_picking_borrado_antes_del_lock_responde_400(self):
        from wms.models import Picking, Transferencia
        from wms.services import cancelacion_service

        picking, _alta, traslado = self._cancelado_con_traslado()
        inexistente = Picking.objects.order_by("-pk").values_list("pk", flat=True).first() + 1000
        # Fuera del lock resuelve un picking que ya no existe cuando se bloquea.
        with patch.object(cancelacion_service, "_picking_id_de", side_effect=[inexistente]):
            resp = self._revertir(traslado)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"transferencia": "La TR cambió mientras se revertía. Intenta de nuevo."})
        self.assertEqual(
            Transferencia.objects.get(pk=traslado.pk).status, Transferencia.TransferenciaStatus.COMPLETADA
        )

    def test_alta_sin_fk_de_picking_trasladado_no_se_revierte_como_manual(self):
        """Alta y traslado sin ``Transferencia.picking`` y ``Picking.transferencia`` = el
        traslado: el alta no resuelve picking, pero su texto dice que es de uno → 400."""
        from wms.models import Transferencia

        picking = self._alta()
        alta = picking.transferencia
        self._trasladar(picking, self.apartados)
        picking.refresh_from_db()
        traslado = picking.transferencia
        Transferencia.objects.filter(pk__in=[alta.pk, traslado.pk]).update(picking=None)
        self.assertEqual(self._cancelar(self.URL_PICKINGS, picking.pk).status_code, 200)
        antes = self._existencias()
        resp = self._revertir(alta)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"transferencia": (
            f"La TR {alta.folio} es de un picking pero no está ligada a él. "
            "No se puede revertir hasta ligarla a su picking."
        )})
        self.assertEqual(self._existencias(), antes)
        self.assertEqual(Transferencia.objects.get(pk=alta.pk).status, Transferencia.TransferenciaStatus.COMPLETADA)

    def test_tr_manual_no_tiene_regla_de_cadena(self):
        """Una TR manual posterior no la bloquea (no hay cadena); sí la regla de
        movimientos ajenos en su destino, como a cualquier TR."""
        vieja = self._tr_manual(self.terminado, self.bordados)
        self._tr_manual(self.terminado, self.apartados)  # posterior, a otro destino
        resp = self._revertir(vieja)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["original"]["status"], "CANCELADA")

        otra = self._tr_manual(self.terminado, self.bordados)
        self._tr_manual(self.terminado, self.bordados)  # entra al mismo destino después
        resp = self._revertir(otra)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("movimientos ajenos", resp.json()["transferencia"])

    def test_otra_empresa_recibe_404_en_los_cuatro(self):
        picking = self._picking_orm("PK-949-Z")
        packing = self._packing_orm("PACK-949-Z", picking)
        despacho = self._despacho_orm(packing)
        tr = self._tr_manual(self.terminado, self.bordados)
        for url, doc_id in ((self.URL_DESPACHOS, despacho.pk), (self.URL_PACKINGS, packing.pk),
                            (self.URL_PICKINGS, picking.pk)):
            with self.subTest(url=url):
                self.assertEqual(self._cancelar(url, doc_id, self.intruso).status_code, 404)
                self.assertEqual(self._api(self.intruso).get(f"{url}{doc_id}/").status_code, 404)
        self.assertEqual(self._revertir(tr, self.intruso).status_code, 404)
        self.assertEqual(self._api(self.intruso).get(f"{self.URL_TR}{tr.pk}/").status_code, 404)


class GenerarOrdenesMuestraTests(TestCase):
    """EC-964: las órdenes de trabajo que genera el picking (OB/OR/OCM) siguen la
    regla del onboarding: una muestra sin SKU no entra, y una con SKU toma talla y
    color de su propia fila del pedido (no tiene variante de catálogo)."""

    @classmethod
    def setUpTestData(cls):
        from catalogo.models import Color, Talla
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente
        from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla
        from wms.models import Picking

        cls.empresa = Empresa.objects.create(codigo="gom", razon_social="Genera OT SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.admin = Usuario.objects.create(
            username="gom-admin", email="gom-admin@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        almacen = Almacen.objects.create(codigo="PT", nombre="PT", empresa=cls.empresa, sucursal=cls.sucursal)
        pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            cliente=Cliente.objects.create(empresa=cls.empresa, nombre="Cliente"),
            moneda=Moneda.objects.create(codigo_iso="MXN", nombre="Peso"),
        )
        cls.picking = Picking.objects.create(
            folio="PK-GOM", empresa=cls.empresa, sucursal=cls.sucursal, pedido=pedido,
            operador=cls.admin, almacen=almacen, usuario=cls.admin,
        )
        cls.color = Color.objects.create(nombre="Amarillo", codigo="AMA", codigo_hex="#ffff00")
        cls.talla_ch = Talla.objects.create(nombre="CH")
        linea = PedidoDetalle.objects.create(
            pedido=pedido, producto=None, producto_nombre_externo="Muestra", color=cls.color,
        )
        cls.con_sku = PedidoDetalleTalla.objects.create(
            pedido_detalle=linea, talla=cls.talla_ch, cantidad=3, sku_muestra="MUE-CH",
        )
        cls.sin_sku = PedidoDetalleTalla.objects.create(
            pedido_detalle=linea, talla=Talla.objects.create(nombre="M"), cantidad=2,
        )

    def _items(self):
        from ventas.models import PedidoDetalleTalla

        tallas = PedidoDetalleTalla.objects.filter(pk__in=[self.con_sku.pk, self.sin_sku.pk]).select_related(
            "talla", "pedido_detalle__producto", "pedido_detalle__color", "variante",
        ).order_by("id")
        return [
            {"talla": t, "cantidad": t.cantidad, "generar_orden_bordado": True,
             "generar_orden_reflejante": True, "generar_orden_corte_manga": True}
            for t in tallas
        ]

    def test_muestra_sin_sku_no_entra_y_con_sku_lleva_su_talla_y_color(self):
        from itertools import count

        from produccion.models import OrdenBordadoDetalle, OrdenCorteMangaDetalle, OrdenReflejanteDetalle
        from wms.services.picking_pipeline.work_orders import generar_ordenes

        folios = count(1)
        with patch("wms.services.picking_pipeline.work_orders.generate_folio_multi_tipo",
                   side_effect=lambda *a, **k: f"OT-{next(folios)}"):
            resultado = generar_ordenes(self.picking, self._items(), self.admin)

        self.assertEqual({r["tipo"] for r in resultado}, {"BORDADO", "REFLEJANTE", "CORTE_MANGA"})
        for modelo in (OrdenBordadoDetalle, OrdenReflejanteDetalle, OrdenCorteMangaDetalle):
            with self.subTest(modelo=modelo.__name__):
                filas = list(modelo.objects.filter(pedido_detalle=self.con_sku.pedido_detalle)
                             .values_list("producto_id", "talla_id", "color_id", "cantidad"))
                self.assertEqual(filas, [(None, self.talla_ch.pk, self.color.pk, 3.0)])

    def test_solo_muestras_sin_sku_no_genera_ordenes(self):
        from wms.services.picking_pipeline.work_orders import generar_ordenes

        items = [it for it in self._items() if it["talla"].pk == self.sin_sku.pk]
        with patch("wms.services.picking_pipeline.work_orders.generate_folio_multi_tipo") as folio:
            self.assertEqual(generar_ordenes(self.picking, items, self.admin), [])
        folio.assert_not_called()


class DespachoEntregadoPorNombreEC979Tests(TestCase):
    """EC-979: ``entregado_por_nombre`` en listado, detalle y respuesta de
    ``entregar/``, con la misma regla que ``cancelado_por_nombre`` (nombre, si no
    el correo; ``null`` sin entrega). Otra empresa: ni la fila ni el 404 la filtran."""

    URL = "/api/v1/wms/despachos/"

    @classmethod
    def setUpTestData(cls):
        from inventarios.models import Almacen
        from nucleo.models import Moneda
        from terceros.models import Cliente
        from ventas.models import Pedido

        cls.empresa = Empresa.objects.create(codigo="ec979", razon_social="EC979 SA")
        cls.otra_empresa = Empresa.objects.create(codigo="ec979-x", razon_social="Otra SA")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="MTZ", nombre="Matriz")
        cls.otra_sucursal = Sucursal.objects.create(empresa=cls.otra_empresa, codigo="GDL", nombre="GDL")
        cls.admin = Usuario.objects.create(
            username="ec979-admin", email="ec979-admin@example.com", first_name="Ana", last_name="López",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        # Sin nombre: ``entregado_por_nombre`` cae al correo.
        cls.sin_nombre = Usuario.objects.create(
            username="ec979-sn", email="ec979-sn@example.com",
            empresa=cls.empresa, sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.intruso = Usuario.objects.create(
            username="ec979-x", email="ec979-x@example.com", first_name="Intruso",
            empresa=cls.otra_empresa, sucursal_default=cls.otra_sucursal, is_admin_empresa=True,
        )
        moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.contexto = {}
        for empresa, sucursal, usuario in (
            (cls.empresa, cls.sucursal, cls.admin), (cls.otra_empresa, cls.otra_sucursal, cls.intruso),
        ):
            almacen = Almacen.objects.create(empresa=empresa, sucursal=sucursal, codigo="PT", nombre="PT")
            pedido = Pedido.objects.create(
                empresa=empresa, sucursal=sucursal, moneda=moneda, folio=f"P-979-{empresa.codigo}",
                cliente=Cliente.objects.create(empresa=empresa, nombre="Cliente"),
            )
            cls.contexto[empresa.pk] = (sucursal, almacen, pedido, usuario)

    def _api(self, user=None):
        api = APIClient()
        api.force_authenticate(user or self.admin)
        return api

    def _despacho(self, empresa=None, entregado_por=None):
        from django.utils import timezone

        from wms.models import Despacho, Packing, Picking

        empresa = empresa or self.empresa
        sucursal, almacen, pedido, usuario = self.contexto[empresa.pk]
        n = Despacho.objects.count() + 1
        picking = Picking.objects.create(
            folio=f"PK-979-{n}", empresa=empresa, sucursal=sucursal, pedido=pedido, operador=usuario,
            almacen=almacen, usuario=usuario,
        )
        packing = Packing.objects.create(
            folio=f"PACK-979-{n}", empresa=empresa, sucursal=sucursal, pedido=pedido, picking=picking,
            operador=usuario, usuario=usuario,
        )
        entrega = {"fecha_entrega": timezone.now(), "entregado_por": entregado_por} if entregado_por else {}
        return Despacho.objects.create(packing=packing, usuario=usuario, **entrega)

    def _filas(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        datos = resp.json()
        return datos.get("results", datos) if isinstance(datos, dict) else datos

    def _entregar(self, despacho, user=None):
        return self._api(user).post(f"{self.URL}{despacho.pk}/entregar/", {"recibio": "Juan"}, format="json")

    def test_entregar_devuelve_entregado_por_nombre(self):
        resp = self._entregar(self._despacho())
        self.assertEqual(resp.status_code, 200, resp.content)
        datos = resp.json()
        self.assertEqual((datos["entregado_por"], datos["entregado_por_nombre"]), (self.admin.pk, "Ana López"))
        self.assertIsNotNone(datos["fecha_entrega"])
        self.assertEqual(datos["recibio"], "Juan")

    def test_entregado_por_nombre_en_listado_y_detalle(self):
        con_nombre = self._despacho()
        sin_nombre = self._despacho()
        pendiente = self._despacho()
        self.assertEqual(self._entregar(con_nombre).status_code, 200)
        self.assertEqual(self._entregar(sin_nombre, self.sin_nombre).status_code, 200)

        filas = {f["id"]: f for f in self._filas(self._api().get(self.URL))}
        casos = (
            (con_nombre, self.admin.pk, "Ana López"),
            (sin_nombre, self.sin_nombre.pk, "ec979-sn@example.com"),
            (pendiente, None, None),
        )
        for despacho, usuario_id, nombre in casos:
            with self.subTest(despacho=despacho.pk):
                detalle = self._api().get(f"{self.URL}{despacho.pk}/").json()
                for fila in (filas[despacho.pk], detalle):
                    self.assertEqual((fila["entregado_por"], fila["entregado_por_nombre"]), (usuario_id, nombre))
                    self.assertEqual(fila["fecha_entrega"] is None, usuario_id is None)

    def test_otra_empresa_no_ve_ni_entrega(self):
        """El despacho entregado de A no sale en el listado de B, su detalle y
        ``entregar/`` son 404 para B, y A tampoco ve el de B."""
        propio = self._despacho(entregado_por=self.admin)
        pendiente = self._despacho()
        ajeno = self._despacho(empresa=self.otra_empresa, entregado_por=self.intruso)

        filas_b = self._filas(self._api(self.intruso).get(self.URL))
        self.assertEqual([f["id"] for f in filas_b], [ajeno.pk])
        self.assertEqual(filas_b[0]["entregado_por_nombre"], "Intruso")
        self.assertEqual(self._api(self.intruso).get(f"{self.URL}{propio.pk}/").status_code, 404)
        self.assertEqual(self._entregar(pendiente, self.intruso).status_code, 404)
        pendiente.refresh_from_db()
        self.assertIsNone(pendiente.fecha_entrega)

        filas_a = {f["id"]: f for f in self._filas(self._api().get(self.URL))}
        self.assertEqual(set(filas_a), {propio.pk, pendiente.pk})
        self.assertNotIn("Intruso", json.dumps(filas_a))
        self.assertEqual(self._api().get(f"{self.URL}{ajeno.pk}/").status_code, 404)

    def test_listado_no_suma_consultas_por_despacho_entregado(self):
        """Cada despacho lo entregó otro usuario: sin ``entregado_por`` en el
        ``select_related`` cada fila sumaría una consulta."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def sembrar(n):
            quien = Usuario.objects.create(
                username=f"ec979-u{n}", email=f"ec979-u{n}@example.com", first_name=f"U{n}",
                empresa=self.empresa, sucursal_default=self.sucursal,
            )
            self._despacho(entregado_por=quien)

        sembrar(0)
        with CaptureQueriesContext(connection) as uno:
            self.assertEqual(len(self._filas(self._api().get(self.URL))), 1)
        for n in range(1, 4):
            sembrar(n)
        with CaptureQueriesContext(connection) as cuatro:
            filas = self._filas(self._api().get(self.URL))
        self.assertEqual(len(filas), 4)
        self.assertEqual({f["entregado_por_nombre"] for f in filas}, {"U0", "U1", "U2", "U3"})
        self.assertEqual(len(cuatro), len(uno))


class LectoresRFIDApiTests(TestCase):
    """``/api/v1/wms/etiquetas-rfid/lectores/``: alta, lista y baja de lectores."""

    URL = "/api/v1/wms/etiquetas-rfid/lectores/"

    @classmethod
    def setUpTestData(cls):
        from seguridad.models import Permiso, UsuarioPermiso

        cls.empresa_a = Empresa.objects.create(codigo="lect-a", razon_social="Empresa A")
        cls.empresa_b = Empresa.objects.create(codigo="lect-b", razon_social="Empresa B")
        cls.sucursal_a = Sucursal.objects.create(empresa=cls.empresa_a, codigo="LA1", nombre="LA1")
        cls.sucursal_a2 = Sucursal.objects.create(empresa=cls.empresa_a, codigo="LA2", nombre="LA2")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.empresa_b, codigo="LB1", nombre="LB1")
        cls.lector_a = LectorRFID.objects.create(empresa=cls.empresa_a, sucursal=cls.sucursal_a, nombre="FX A")
        cls.lector_a2 = LectorRFID.objects.create(empresa=cls.empresa_a, sucursal=cls.sucursal_a2, nombre="FX A2")
        cls.lector_b = LectorRFID.objects.create(empresa=cls.empresa_b, sucursal=cls.sucursal_b, nombre="FX B")
        cls.admin_a = Usuario.objects.create(
            username="lect-admin-a", email="lect-admin-a@example.com", empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a, is_admin_empresa=True,
        )
        cls.admin_b = Usuario.objects.create(
            username="lect-admin-b", email="lect-admin-b@example.com", empresa=cls.empresa_b,
            sucursal_default=cls.sucursal_b, is_admin_empresa=True,
        )
        cls.lector_usuario = Usuario.objects.create(
            username="lect-scan-a", email="lect-scan-a@example.com", empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a,
        )
        permiso, _ = Permiso.objects.get_or_create(clave="R-WMS-SCANNER", defaults={"nombre": "R-WMS-SCANNER"})
        UsuarioPermiso.objects.create(
            usuario=cls.lector_usuario, permiso=permiso, tipo=UsuarioPermiso.TIPO_GRANT, empresa=cls.empresa_a,
        )
        cls.sin_permiso = Usuario.objects.create(
            username="lect-nada-a", email="lect-nada-a@example.com", empresa=cls.empresa_a,
            sucursal_default=cls.sucursal_a,
        )

    def _api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _post_fx(self, token):
        return self.client.post(
            f"/api/receive-scan/?token={token}",
            data=json.dumps([{"epcId": "AAAABBBBCCCCDDDDEEEE7777"}]),
            content_type="application/json",
        )

    # ── Lista ─────────────────────────────────────────────────────────

    def test_lista_solo_empresa_activa_con_url_fx_y_estado(self):
        resp = self._api(self.admin_a).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        filas = resp.json()
        self.assertEqual([f["nombre"] for f in filas], ["FX A", "FX A2"])
        fila = filas[0]
        # El token solo existe como hash: en la lista va enmascarado.
        self.assertTrue(fila["url_fx"].endswith(f"/api/receive-scan/?token=••••{self.lector_a.token[-4:]}"))
        self.assertNotIn(self.lector_a.token, json.dumps(filas))
        self.assertEqual(fila["estado"], "SIN_CONEXION")
        self.assertIsNone(fila["ultima_conexion"])

    def test_lista_paginada_con_limit(self):
        data = self._api(self.admin_a).get(self.URL + "?limit=1").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["results"]), 1)

    def test_lista_consultas_constantes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as antes:
            self._api(self.admin_a).get(self.URL)
        for i in range(10):
            LectorRFID.objects.create(empresa=self.empresa_a, sucursal=self.sucursal_a, nombre=f"extra-{i}")
        with CaptureQueriesContext(connection) as despues:
            self._api(self.admin_a).get(self.URL)
        self.assertEqual(len(antes), len(despues))

    def test_estado_leyendo_tras_lectura_del_fx(self):
        self.assertEqual(self._post_fx(self.lector_a.token).status_code, 200)
        fila = self._api(self.admin_a).get(f"{self.URL}{self.lector_a.pk}/").json()
        self.assertEqual(fila["estado"], "LEYENDO")
        self.assertIsNotNone(fila["ultima_lectura"])

    def test_no_admin_con_permiso_ve_solo_sus_sucursales(self):
        resp = self._api(self.lector_usuario).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([f["nombre"] for f in resp.json()], ["FX A"])

    def test_sin_r_wms_scanner_403(self):
        self.assertEqual(self._api(self.sin_permiso).get(self.URL).status_code, 403)

    def test_otra_empresa_404(self):
        cliente = self._api(self.admin_b)
        self.assertEqual(cliente.get(f"{self.URL}{self.lector_a.pk}/").status_code, 404)
        self.assertEqual(cliente.delete(f"{self.URL}{self.lector_a.pk}/").status_code, 404)

    # ── Alta ──────────────────────────────────────────────────────────

    def test_alta_genera_token_y_url_fx(self):
        resp = self._api(self.admin_a).post(self.URL, {"nombre": "Anden 2", "ubicacion": "Puerta 2"}, format="json")
        self.assertEqual(resp.status_code, 201)
        lector = LectorRFID.objects.get(pk=resp.json()["id"])
        self.assertEqual(lector.empresa_id, self.empresa_a.pk)
        self.assertEqual(lector.sucursal_id, self.sucursal_a.pk)  # sucursal por defecto del usuario
        self.assertEqual(resp.json()["ubicacion"], "Puerta 2")
        # Al crear, la única vez que se ve completo; en la BD solo queda su hash.
        token = resp.json()["url_fx"].split("token=")[1]
        self.assertEqual(lector.token_hash, hash_token_lector(token))
        self.assertEqual(self._post_fx(token).status_code, 200)

    def test_alta_nombre_duplicado_400_sin_importar_mayusculas(self):
        resp = self._api(self.admin_a).post(self.URL, {"nombre": "fx a"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nombre", resp.json())

    def test_alta_mismo_nombre_en_otra_empresa_si_vale(self):
        resp = self._api(self.admin_b).post(self.URL, {"nombre": "FX A"}, format="json")
        self.assertEqual(resp.status_code, 201)

    def test_alta_sucursal_de_otra_empresa_400(self):
        resp = self._api(self.admin_a).post(
            self.URL, {"nombre": "Nuevo", "sucursal": self.sucursal_b.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sucursal", resp.json())

    def test_no_admin_no_crea_ni_elimina(self):
        cliente = self._api(self.lector_usuario)
        self.assertEqual(cliente.post(self.URL, {"nombre": "x"}, format="json").status_code, 403)
        self.assertEqual(cliente.delete(f"{self.URL}{self.lector_a.pk}/").status_code, 403)

    # ── Baja (soft delete) ────────────────────────────────────────────

    def test_baja_invalida_token_y_conserva_lecturas(self):
        self.assertEqual(self._post_fx(self.lector_a.token).status_code, 200)
        cliente = self._api(self.admin_a)

        self.assertEqual(cliente.delete(f"{self.URL}{self.lector_a.pk}/").status_code, 204)

        self.assertEqual(self._post_fx(self.lector_a.token).status_code, 401)
        self.assertEqual(RfidScan.objects.filter(lector=self.lector_a).count(), 1)
        self.assertNotIn(self.lector_a.pk, [f["id"] for f in cliente.get(self.URL).json()])
        self.assertEqual(cliente.get(f"{self.URL}{self.lector_a.pk}/").status_code, 404)

    def test_baja_libera_el_nombre(self):
        cliente = self._api(self.admin_a)
        cliente.delete(f"{self.URL}{self.lector_a.pk}/")
        self.assertEqual(cliente.post(self.URL, {"nombre": "FX A"}, format="json").status_code, 201)

    def test_receive_scan_token_inventado_o_sin_token_401(self):
        self.assertEqual(self._post_fx("no-existe").status_code, 401)
        resp = self.client.post(
            "/api/receive-scan/", data=json.dumps([{"epcId": "AAAABBBBCCCCDDDDEEEE7778"}]),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(RfidScan.objects.count(), 0)


class LectorRFIDEscaneoPorLectorTests(TestCase):
    """``scans/?lector=`` con ``lector_id``/``lector_nombre`` y el aviso "en uso".

    Mismos datos que ``LectoresRFIDApiTests`` sin heredar (y repetir) sus tests."""

    URL = LectoresRFIDApiTests.URL
    setUpTestData = classmethod(LectoresRFIDApiTests.setUpTestData.__func__)
    _api = LectoresRFIDApiTests._api
    SCANS_URL = "/api/v1/wms/etiquetas-rfid/scans/"

    def _sembrar(self, lector, n, prefijo):
        RfidScan.objects.bulk_create(
            [RfidScan(empresa=lector.empresa, lector=lector, epc=f"{prefijo}{i:08d}") for i in range(n)]
        )

    def _en_uso(self, user, lector, **datos):
        return self._api(user).post(f"{self.URL}{lector.pk}/en-uso/", datos, format="json")

    # ── scans/?lector= ────────────────────────────────────────────────

    def test_scans_traen_lector_id_y_nombre(self):
        self._sembrar(self.lector_a, 1, "aaaa")
        scan = self._api(self.admin_a).get(self.SCANS_URL).json()["scans"][0]
        self.assertEqual(scan["lector_id"], self.lector_a.pk)
        self.assertEqual(scan["lector_nombre"], "FX A")

    def test_scans_filtra_por_lector(self):
        self._sembrar(self.lector_a, 2, "aaaa")
        self._sembrar(self.lector_a2, 3, "a2a2")
        cliente = self._api(self.admin_a)
        self.assertEqual(len(cliente.get(self.SCANS_URL).json()["scans"]), 5)
        solo = cliente.get(f"{self.SCANS_URL}?lector={self.lector_a2.pk}").json()["scans"]
        self.assertEqual({s["lector_id"] for s in solo}, {self.lector_a2.pk})
        self.assertEqual(len(solo), 3)

    def test_scans_lector_combinable_con_unicos(self):
        from django.utils import timezone

        RfidScan.objects.bulk_create(
            [RfidScan(empresa=self.empresa_a, lector=self.lector_a, epc="REPETIDO00001") for _ in range(3)]
            + [RfidScan(empresa=self.empresa_a, lector=self.lector_a2, epc="REPETIDO00001")]
        )
        # ``unicos`` sale de ``RfidLecturaUltima`` (#1460), que llena ``receive``: una
        # fila por lector y EPC con su última lectura.
        for lector in (self.lector_a, self.lector_a2):
            ultima = RfidScan.objects.filter(lector=lector).order_by("-id").first()
            RfidLecturaUltima.objects.create(
                empresa=self.empresa_a, lector=lector, epc=ultima.epc, scan=ultima, visto=timezone.now(),
            )
        url = f"{self.SCANS_URL}?lector={self.lector_a.pk}&unicos=1"
        scans = self._api(self.admin_a).get(url).json()["scans"]
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["lector_id"], self.lector_a.pk)

    def test_scans_lector_no_numerico_400(self):
        resp = self._api(self.admin_a).get(f"{self.SCANS_URL}?lector=abc")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("lector", resp.json())

    def test_scans_lector_no_agrega_consultas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._sembrar(self.lector_a, 5, "aaaa")
        cliente = self._api(self.admin_a)
        with CaptureQueriesContext(connection) as sin:
            cliente.get(self.SCANS_URL)
        with CaptureQueriesContext(connection) as con:
            cliente.get(f"{self.SCANS_URL}?lector={self.lector_a.pk}")
        self.assertEqual(len(sin), len(con))

    # ── En uso ────────────────────────────────────────────────────────

    def test_en_uso_lo_manda_quien_tiene_r_wms_scanner(self):
        resp = self._en_uso(self.lector_usuario, self.lector_a, pantalla="Recepcion", documento="REC-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["en_uso_por"]["pantalla"], "Recepcion")

        fila = self._api(self.admin_a).get(f"{self.URL}{self.lector_a.pk}/").json()
        self.assertEqual(fila["en_uso_por"]["usuario"], self.lector_usuario.email)  # sin nombre: su correo
        self.assertEqual(fila["en_uso_por"]["documento"], "REC-1")

    def test_en_uso_sin_permiso_403(self):
        self.assertEqual(self._en_uso(self.sin_permiso, self.lector_a).status_code, 403)

    def test_en_uso_otra_empresa_404(self):
        self.assertEqual(self._en_uso(self.admin_b, self.lector_a).status_code, 404)

    def test_en_uso_conserva_desde_mientras_late(self):
        primero = self._en_uso(self.lector_usuario, self.lector_a, pantalla="Recepcion").json()
        segundo = self._en_uso(self.lector_usuario, self.lector_a, pantalla="Recepcion").json()
        self.assertEqual(primero["en_uso_por"]["desde"], segundo["en_uso_por"]["desde"])

    def test_en_uso_expira_sin_latido(self):
        from datetime import timedelta

        from django.utils import timezone

        self._en_uso(self.lector_usuario, self.lector_a, pantalla="Recepcion")
        LectorRFID.objects.filter(pk=self.lector_a.pk).update(
            en_uso_latido=timezone.now() - timedelta(seconds=61)
        )
        fila = self._api(self.admin_a).get(f"{self.URL}{self.lector_a.pk}/").json()
        self.assertIsNone(fila["en_uso_por"])

    def test_en_uso_otro_usuario_lo_toma(self):
        self._en_uso(self.lector_usuario, self.lector_a, pantalla="Recepcion")
        self._en_uso(self.admin_a, self.lector_a, pantalla="Surtido")
        fila = self._api(self.admin_a).get(f"{self.URL}{self.lector_a.pk}/").json()
        self.assertEqual(fila["en_uso_por"]["pantalla"], "Surtido")

    def test_en_uso_pantalla_larga_400(self):
        self.assertEqual(self._en_uso(self.admin_a, self.lector_a, pantalla="x" * 51).status_code, 400)


class LectorRFIDBlindajeTests(TestCase):
    """Token con hash, límites de ``receive-scan``, dedupe, ``unicos`` y retención."""

    URL = LectoresRFIDApiTests.URL
    SCANS_URL = "/api/v1/wms/etiquetas-rfid/scans/"
    setUpTestData = classmethod(LectoresRFIDApiTests.setUpTestData.__func__)
    _api = LectoresRFIDApiTests._api

    def _post(self, token, payload):
        return self.client.post(
            f"/api/receive-scan/?token={token}",
            data=payload if isinstance(payload, str) else json.dumps(payload),
            content_type="application/json",
        )

    # ── Token ─────────────────────────────────────────────────────────

    def test_bd_no_guarda_el_token_en_claro(self):
        fila = LectorRFID.objects.filter(pk=self.lector_a.pk).values().get()
        self.assertNotIn(self.lector_a.token, json.dumps(fila, default=str))
        self.assertEqual(fila["token_hash"], hash_token_lector(self.lector_a.token))

    def test_regenerar_token_invalida_el_anterior(self):
        viejo = self.lector_a.token
        resp = self._api(self.admin_a).post(f"{self.URL}{self.lector_a.pk}/regenerar-token/")
        self.assertEqual(resp.status_code, 200)
        nuevo = resp.json()["url_fx"].split("token=")[1]
        self.assertNotEqual(nuevo, viejo)
        self.assertEqual(self._post(viejo, [{"epc": "AAAABBBBCCCC0001"}]).status_code, 401)
        self.assertEqual(self._post(nuevo, [{"epc": "AAAABBBBCCCC0001"}]).status_code, 200)

    def test_regenerar_token_solo_admin(self):
        resp = self._api(self.lector_usuario).post(f"{self.URL}{self.lector_a.pk}/regenerar-token/")
        self.assertEqual(resp.status_code, 403)

    # ── Límites de receive-scan ───────────────────────────────────────

    def test_payload_grande_413(self):
        resp = self._post(self.lector_a.token, "x" * (1024 * 1024 + 1))
        self.assertEqual(resp.status_code, 413)

    def test_demasiados_tags_413(self):
        resp = self._post(self.lector_a.token, [{"epc": f"AAAA{i:012X}"} for i in range(2001)])
        self.assertEqual(resp.status_code, 413)

    def test_rate_limit_429(self):
        from django.utils import timezone

        LectorRFID.objects.filter(pk=self.lector_a.pk).update(rl_conteo=600, rl_ventana=timezone.now())
        self.assertEqual(self._post(self.lector_a.token, []).status_code, 429)

    def test_rate_limit_se_reinicia_pasado_el_minuto(self):
        from datetime import timedelta

        from django.utils import timezone

        LectorRFID.objects.filter(pk=self.lector_a.pk).update(
            rl_conteo=600, rl_ventana=timezone.now() - timedelta(minutes=2)
        )
        self.assertEqual(self._post(self.lector_a.token, []).status_code, 200)

    def test_antena_y_rssi_fuera_de_rango_se_guardan_nulos(self):
        self._post(self.lector_a.token, [{"epc": "AAAABBBBCCCC0002", "antenna": 500, "rssi": 999}])
        scan = RfidScan.objects.get()
        self.assertIsNone(scan.antenna)
        self.assertIsNone(scan.rssi)

    def test_respuesta_sin_debug(self):
        data = self._post(self.lector_a.token, [{"epc": "AAAABBBBCCCC0003"}]).json()
        self.assertEqual(set(data), {"status", "count", "duplicadas"})

    # ── Dedupe y unicos ───────────────────────────────────────────────

    def test_misma_etiqueta_en_la_ventana_no_se_guarda_dos_veces(self):
        lote = [{"epc": "AAAABBBBCCCC0004"}, {"epc": "AAAABBBBCCCC0005"}]
        self.assertEqual(self._post(self.lector_a.token, lote).json()["count"], 2)
        segundo = self._post(self.lector_a.token, lote).json()
        self.assertEqual((segundo["count"], segundo["duplicadas"]), (0, 2))
        self.assertEqual(RfidScan.objects.count(), 2)
        self.assertEqual(RfidLecturaUltima.objects.count(), 2)

    def test_otro_lector_si_guarda_la_misma_etiqueta(self):
        lote = [{"epc": "AAAABBBBCCCC0006"}]
        self._post(self.lector_a.token, lote)
        self.assertEqual(self._post(self.lector_a2.token, lote).json()["count"], 1)

    def test_pasada_la_ventana_se_vuelve_a_guardar(self):
        from datetime import timedelta

        from django.utils import timezone

        lote = [{"epc": "AAAABBBBCCCC0007"}]
        self._post(self.lector_a.token, lote)
        RfidLecturaUltima.objects.update(visto=timezone.now() - timedelta(seconds=31))
        self.assertEqual(self._post(self.lector_a.token, lote).json()["count"], 1)
        self.assertEqual(RfidLecturaUltima.objects.count(), 1)  # upsert, no otra fila

    def test_unicos_por_lector_usa_la_ultima_lectura(self):
        from datetime import timedelta

        from django.utils import timezone

        lote = [{"epc": "AAAABBBBCCCC0008"}]
        self._post(self.lector_a.token, lote)
        RfidLecturaUltima.objects.update(visto=timezone.now() - timedelta(seconds=31))
        self._post(self.lector_a.token, lote)
        url = f"{self.SCANS_URL}?lector={self.lector_a.pk}&unicos=1"
        scans = self._api(self.admin_a).get(url).json()["scans"]
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["id"], RfidLecturaUltima.objects.get().scan_id)

    # ── Aislamiento ───────────────────────────────────────────────────

    def test_scans_lector_de_otra_empresa_404(self):
        resp = self._api(self.admin_a).get(f"{self.SCANS_URL}?lector={self.lector_b.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_scans_lector_de_sucursal_ajena_404(self):
        resp = self._api(self.lector_usuario).get(f"{self.SCANS_URL}?lector={self.lector_a2.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_scans_lector_propio_sin_lecturas_200(self):
        resp = self._api(self.admin_a).get(f"{self.SCANS_URL}?lector={self.lector_a.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["scans"], [])

    # ── Retención ─────────────────────────────────────────────────────

    def test_purga_lecturas_de_mas_de_30_dias(self):
        from datetime import timedelta

        from django.core.cache import cache
        from django.utils import timezone

        from wms.services.rfid_scan_service import purgar_lecturas_viejas

        self._post(self.lector_a.token, [{"epc": "AAAABBBBCCCC0009"}, {"epc": "AAAABBBBCCCC0010"}])
        vieja = RfidScan.objects.get(epc="AAAABBBBCCCC0009")
        RfidScan.objects.filter(pk=vieja.pk).update(created_at=timezone.now() - timedelta(days=31))
        cache.delete("wms:rfid:purga_lecturas")
        self.assertEqual(purgar_lecturas_viejas(), 1)
        self.assertEqual(list(RfidScan.objects.values_list("epc", flat=True)), ["AAAABBBBCCCC0010"])
        self.assertEqual(RfidLecturaUltima.objects.count(), 1)


class EtiquetasRFIDListadoDetalleEC989Tests(TestCase):
    """EC-989: listado ligero (sin ``etiquetas`` ni ``zpl_enviado``) y paginación
    opcional, detalle completo, ``cantidad`` de GET ``onboarding/``/``preview/`` con
    el tope y el mensaje del POST, y alcance por empresa activa."""

    URL = "/api/v1/wms/etiquetas-rfid/"
    CAMPOS_LISTA = [
        "id", "folio", "empresa", "sucursal", "usuario", "producto", "producto_variante",
        "producto_nombre", "producto_variante_nombre", "sku", "codigo_producto", "cantidad",
        "rfid_mode", "printer_name", "printer_address", "status", "observaciones",
        "created_at", "updated_at",
    ]
    EPC = "000015D80438000188F0D54F"
    ZPL_VIEJO = "\n".join([
        "^XA", "^FO40,245^BCN,90,Y,N,N^FDPB01^FS",
        "^RB96,,,1", "^RS8,E", "^RFW,E", f"^FD{EPC}^FS",
        "^FO40,360^A0N,18,18^FDEPC^FS", "^XZ",
    ])
    # Rechazadas igual que en el POST: arriba del tope, cero, negativa, texto,
    # decimal y un dígito Unicode (antes 500 por ``str.isdigit``).
    INVALIDAS = ["10001", "0", "-1", "abc", "1.5", "²"]

    @classmethod
    def setUpTestData(cls):
        cls.empresa = Empresa.objects.create(codigo="ec989-a", razon_social="Empresa A")
        cls.otra_empresa = Empresa.objects.create(codigo="ec989-b", razon_social="Empresa B")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="A1", nombre="A1")
        cls.sucursal_2 = Sucursal.objects.create(empresa=cls.empresa, codigo="A2", nombre="A2")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.otra_empresa, codigo="B1", nombre="B1")
        cls.producto = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera", codigo="PB01"
        )
        cls.producto_b = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.otra_empresa, nombre="Gorra", codigo="GB01"
        )
        cls.admin = Usuario.objects.create(
            username="ec989-admin", email="ec989-admin@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.operador = Usuario.objects.create(
            username="ec989-op", email="ec989-op@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal,
        )
        cls.superusuario_b = Usuario.objects.create(
            username="ec989-su", email="ec989-su@example.com", empresa=cls.otra_empresa,
            is_superuser=True, is_staff=True,
        )
        cls.sin_empresa = Usuario.objects.create(username="ec989-nadie", email="ec989-nadie@example.com")

        cls.impresion = EtiquetaRFIDImpresion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, usuario=cls.admin, producto=cls.producto,
            cantidad=2, zpl_enviado=cls.ZPL_VIEJO,
        )
        EtiquetaRFIDDetalle.objects.create(
            impresion=cls.impresion, epc=cls.EPC, barcode_value="PB01", serial="0001"
        )
        EtiquetaRFIDDetalle.objects.create(
            impresion=cls.impresion, epc="000015D80438000288F0D54F", barcode_value="PB01", serial="0002"
        )
        cls.impresion_2 = EtiquetaRFIDImpresion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal_2, producto=cls.producto, cantidad=1, rfid_mode=False,
        )
        cls.impresion_3 = EtiquetaRFIDImpresion.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal, producto=cls.producto, cantidad=1, rfid_mode=False,
        )
        cls.impresion_b = EtiquetaRFIDImpresion.objects.create(
            empresa=cls.otra_empresa, sucursal=cls.sucursal_b, producto=cls.producto_b, cantidad=1,
        )
        EtiquetaRFIDDetalle.objects.create(
            impresion=cls.impresion_b, epc="BBBB15D80438000188F0D54F", barcode_value="GB01"
        )

    def _api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    # ── Listado ───────────────────────────────────────────────────────

    def test_listado_sin_etiquetas_ni_zpl_como_arreglo(self):
        with self.assertNumQueries(1):
            resp = self._api(self.admin).get(self.URL)
        self.assertEqual(resp.status_code, 200)
        datos = resp.json()
        self.assertIsInstance(datos, list)
        self.assertEqual(
            [f["id"] for f in datos], [self.impresion_3.pk, self.impresion_2.pk, self.impresion.pk]
        )
        for fila in datos:
            self.assertEqual(list(fila), self.CAMPOS_LISTA)
        fila = datos[-1]
        self.assertEqual(fila["folio"], f"LAB-{self.impresion.pk:06d}")
        self.assertEqual(fila["producto_nombre"], "Playera")
        self.assertEqual(fila["codigo_producto"], "PB01")
        self.assertEqual(fila["cantidad"], 2)

    def test_listado_paginado_con_limit_offset(self):
        with self.assertNumQueries(2):
            resp = self._api(self.admin).get(self.URL, {"limit": 2, "offset": 1})
        self.assertEqual(resp.status_code, 200)
        datos = resp.json()
        self.assertEqual(list(datos), ["count", "next", "previous", "results"])
        self.assertEqual(datos["count"], 3)
        self.assertEqual([f["id"] for f in datos["results"]], [self.impresion_2.pk, self.impresion.pk])
        self.assertEqual(list(datos["results"][0]), self.CAMPOS_LISTA)
        self.assertEqual(self._api(self.admin).get(self.URL, {"limit": "x"}).status_code, 400)

    def test_listado_operador_solo_sus_sucursales(self):
        datos = self._api(self.operador).get(self.URL).json()
        self.assertEqual([f["id"] for f in datos], [self.impresion_3.pk, self.impresion.pk])

    # ── Detalle ───────────────────────────────────────────────────────

    def test_detalle_con_epc_y_zpl_vigente(self):
        from wms.services.rfid_label_service import zpl_rfid_vigente

        with self.assertNumQueries(2):
            resp = self._api(self.admin).get(f"{self.URL}{self.impresion.pk}/")
        self.assertEqual(resp.status_code, 200)
        datos = resp.json()
        self.assertEqual(
            [c for c in datos if c not in ("etiquetas", "zpl_enviado")], self.CAMPOS_LISTA
        )
        self.assertEqual(datos["zpl_enviado"], zpl_rfid_vigente(self.ZPL_VIEJO))
        self.assertNotEqual(datos["zpl_enviado"], self.ZPL_VIEJO)
        self.assertEqual(
            [(e["epc"], e["serial"]) for e in datos["etiquetas"]],
            [(self.EPC, "0001"), ("000015D80438000288F0D54F", "0002")],
        )
        self.assertEqual(
            sorted(datos["etiquetas"][0]),
            sorted([
                "id", "impresion", "epc", "barcode_value", "serial", "estado", "leida_at",
                "created_at", "updated_at",
            ]),
        )

    # ── Alcance ───────────────────────────────────────────────────────

    def test_otra_empresa_404_en_detalle_y_fuera_del_listado(self):
        api = self._api(self.admin)
        self.assertEqual(api.get(f"{self.URL}{self.impresion_b.pk}/").status_code, 404)
        self.assertNotIn(self.impresion_b.pk, [f["id"] for f in api.get(self.URL).json()])
        self.assertNotIn(
            self.impresion_b.pk, [f["id"] for f in api.get(self.URL, {"limit": 10}).json()["results"]]
        )

    def test_superusuario_con_otra_empresa_activa(self):
        api = self._api(self.superusuario_b)
        self.assertEqual([f["id"] for f in api.get(self.URL).json()], [self.impresion_b.pk])
        self.assertEqual(api.get(f"{self.URL}{self.impresion.pk}/").status_code, 404)
        self.assertEqual(api.get(f"{self.URL}{self.impresion_b.pk}/").status_code, 200)

    def test_usuario_sin_empresa_no_ve_nada(self):
        api = self._api(self.sin_empresa)
        self.assertEqual(api.get(self.URL).json(), [])
        self.assertEqual(api.get(self.URL, {"limit": 5}).json()["count"], 0)
        self.assertEqual(api.get(f"{self.URL}{self.impresion.pk}/").status_code, 404)

    # ── cantidad en GET onboarding/ y preview/ ───────────────────────

    def test_preview_rechaza_cantidad_invalida(self):
        api = self._api(self.admin)
        for valor in self.INVALIDAS:
            with self.subTest(cantidad=valor):
                resp = api.get(f"{self.URL}preview/", {"producto": self.producto.pk, "cantidad": valor})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(list(resp.json()), ["cantidad"])

    def test_onboarding_rechaza_cantidad_invalida_con_motivo(self):
        api = self._api(self.admin)
        for valor in self.INVALIDAS:
            with self.subTest(cantidad=valor):
                resp = api.get(f"{self.URL}onboarding/", {"producto": self.producto.pk, "cantidad": valor})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(list(resp.json()), ["cantidad"])
                self.assertNotIn("Solicitud inválida", resp.content.decode())

    def test_get_y_post_rechazan_con_el_mismo_mensaje(self):
        from wms.services.rfid_label_service import MAX_CANTIDAD_ETIQUETAS

        api = self._api(self.admin)
        arriba = MAX_CANTIDAD_ETIQUETAS + 1
        post = api.post(self.URL, {"producto": self.producto.pk, "cantidad": arriba}, format="json")
        preview = api.get(f"{self.URL}preview/", {"producto": self.producto.pk, "cantidad": arriba})
        onboarding = api.get(f"{self.URL}onboarding/", {"producto": self.producto.pk, "cantidad": arriba})
        self.assertEqual(post.status_code, 400)
        self.assertIn(str(MAX_CANTIDAD_ETIQUETAS), post.json()["cantidad"][0])
        self.assertEqual(preview.json(), {"cantidad": post.json()["cantidad"]})
        self.assertEqual(onboarding.json(), {"cantidad": post.json()["cantidad"]})
        self.assertFalse(EtiquetaRFIDImpresion.objects.filter(cantidad=arriba).exists())

    def test_post_usa_el_tope_compartido(self):
        from wms.services.rfid_label_service import MAX_CANTIDAD_ETIQUETAS

        campo = EtiquetaRFIDCreateSerializer().fields["cantidad"]
        self.assertEqual((campo.min_value, campo.max_value), (1, MAX_CANTIDAD_ETIQUETAS))
        ok = EtiquetaRFIDCreateSerializer(data={"producto": self.producto.pk, "cantidad": MAX_CANTIDAD_ETIQUETAS})
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_cantidad_valida_o_ausente_sigue_igual(self):
        api = self._api(self.admin)
        resp = api.get(f"{self.URL}preview/", {"producto": self.producto.pk, "cantidad": 3})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cantidad"], 3)
        self.assertEqual(len(resp.json()["zpl_individual"]), 3)
        for params in ({}, {"cantidad": ""}):
            with self.subTest(params=params):
                resp = api.get(f"{self.URL}onboarding/", {"producto": self.producto.pk, **params})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["preview"]["cantidad"], 1)

    def test_onboarding_otros_errores_conservan_el_mensaje_generico(self):
        resp = self._api(self.admin).get(f"{self.URL}onboarding/", {"producto": self.producto_b.pk})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "Solicitud inválida. Verifica los datos enviados.")

    def test_acciones_propias_no_se_paginan(self):
        api = self._api(self.admin)
        buscar = api.get(f"{self.URL}buscar/", {"q": "Play", "limit": 1}).json()
        self.assertEqual(sorted(buscar), ["q", "resultados", "sucursal_ids"])
        onboarding = api.get(f"{self.URL}onboarding/", {"limit": 1}).json()
        self.assertNotIn("results", onboarding)
        self.assertIn("resultados", onboarding)
        preview = api.get(f"{self.URL}preview/", {"producto": self.producto.pk, "limit": 1}).json()
        self.assertIn("zpl_individual", preview)
        self.assertIn("scans", api.get(f"{self.URL}scans/", {"limit": 1}).json())


class EtiquetasRFIDListadoServidorEC989Tests(TestCase):
    """EC-989 (modo servidor del historial): ``?search=`` (folio, SKU, producto,
    variante), ``?status=`` y ``?ordering=`` en el listado, combinados con
    ``?limit=&offset=``, siempre dentro de la empresa activa."""

    URL = "/api/v1/wms/etiquetas-rfid/"

    @classmethod
    def setUpTestData(cls):
        from datetime import timedelta

        from django.utils import timezone

        from catalogo.models import Color, ProductoVariante, Talla

        cls.empresa = Empresa.objects.create(codigo="ec989s-a", razon_social="Empresa A")
        cls.otra_empresa = Empresa.objects.create(codigo="ec989s-b", razon_social="Empresa B")
        cls.sucursal = Sucursal.objects.create(empresa=cls.empresa, codigo="SA1", nombre="SA1")
        cls.sucursal_b = Sucursal.objects.create(empresa=cls.otra_empresa, codigo="SB1", nombre="SB1")
        color = Color.objects.create(nombre="AZUL EC989", codigo="AZ9", codigo_hex="#0000FF")
        talla = Talla.objects.create(nombre="M EC989")
        cls.polo = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.empresa, nombre="Playera Polo", codigo="PP01"
        )
        cls.gorra = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.empresa, nombre="Gorra Trucker", codigo="GT01"
        )
        cls.variante = ProductoVariante.objects.create(
            producto=cls.polo, empresa=cls.empresa, color=color, talla=talla,
            nombre="Polo azul mediana", sku="PP01-AZ-M", precio_base=Decimal("100"),
        )
        # El nombre compuesto lo puede reescribir ``save()``: se fija sin pasar por él.
        ProductoVariante.objects.filter(pk=cls.variante.pk).update(nombre="Polo azul mediana")
        polo_b = Producto.objects.create(
            tipo=_tipo_producto(), empresa=cls.otra_empresa, nombre="Playera Polo B", codigo="PP01B"
        )
        variante_b = ProductoVariante.objects.create(
            producto=polo_b, empresa=cls.otra_empresa, color=color, talla=talla,
            nombre="Polo azul mediana B", sku="PP01-AZ-B", precio_base=Decimal("100"),
        )
        cls.admin = Usuario.objects.create(
            username="ec989s-admin", email="ec989s-admin@example.com", empresa=cls.empresa,
            sucursal_default=cls.sucursal, is_admin_empresa=True,
        )
        cls.superusuario_b = Usuario.objects.create(
            username="ec989s-su", email="ec989s-su@example.com", empresa=cls.otra_empresa,
            is_superuser=True, is_staff=True,
        )

        def impresion(empresa, sucursal, producto, variante, status, cantidad, horas):
            obj = EtiquetaRFIDImpresion.objects.create(
                empresa=empresa, sucursal=sucursal, producto=producto, producto_variante=variante,
                status=status, cantidad=cantidad, rfid_mode=False,
            )
            # ``created_at`` fijo: el orden por fecha no depende de microsegundos.
            EtiquetaRFIDImpresion.objects.filter(pk=obj.pk).update(
                created_at=timezone.now() - timedelta(hours=horas)
            )
            return obj

        E = EtiquetaRFIDImpresion.Estatus
        cls.i1 = impresion(cls.empresa, cls.sucursal, cls.polo, cls.variante, E.EXITO, 5, 40)
        cls.i2 = impresion(cls.empresa, cls.sucursal, cls.gorra, None, E.FALLIDO, 1, 30)
        cls.i3 = impresion(cls.empresa, cls.sucursal, cls.polo, cls.variante, E.PENDIENTE, 3, 20)
        cls.i4 = impresion(cls.empresa, cls.sucursal, cls.gorra, None, E.EXITO, 10, 10)
        cls.ib = impresion(cls.otra_empresa, cls.sucursal_b, polo_b, variante_b, E.EXITO, 7, 5)

    def _ids(self, params, user=None):
        client = APIClient()
        client.force_authenticate(user or self.admin)
        resp = client.get(self.URL, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        datos = resp.json()
        filas = datos["results"] if isinstance(datos, dict) else datos
        return [f["id"] for f in filas]

    # ── search ────────────────────────────────────────────────────────

    def test_search_por_folio(self):
        self.assertEqual(self._ids({"search": f"LAB-{self.i3.pk:06d}"}), [self.i3.pk])
        # Sin distinguir mayúsculas; sólo las de la empresa activa.
        self.assertEqual(
            self._ids({"search": "lab-"}), [self.i4.pk, self.i3.pk, self.i2.pk, self.i1.pk]
        )

    def test_folio_sql_igual_al_de_python(self):
        """La expresión de búsqueda arma el mismo texto que ``folio`` (``LAB-{pk:06d}``),
        también arriba de 6 dígitos, donde ``LPAD`` recortaría."""
        from django.db.models import IntegerField, Value

        from wms.api.etiquetas_rfid_listado import folio_sql

        for pk in (1, 42, 999999, 1000000, 12345678):
            with self.subTest(pk=pk):
                folio = (
                    EtiquetaRFIDImpresion.objects.filter(pk=self.i1.pk)
                    .annotate(f=folio_sql(Value(pk, output_field=IntegerField())))
                    .values_list("f", flat=True)
                    .get()
                )
                self.assertEqual(folio, f"LAB-{pk:06d}")
        self.assertEqual(self.i1.folio, f"LAB-{self.i1.pk:06d}")

    def test_search_por_sku_producto_y_variante(self):
        self.assertEqual(self._ids({"search": "pp01-az"}), [self.i3.pk, self.i1.pk])
        self.assertEqual(self._ids({"search": "GORRA"}), [self.i4.pk, self.i2.pk])
        self.assertEqual(self._ids({"search": "azul mediana"}), [self.i3.pk, self.i1.pk])
        self.assertEqual(self._ids({"search": "no-existe"}), [])

    def test_search_no_cruza_empresas(self):
        # ``PP01-AZ-B`` y "Polo azul mediana B" son de la otra empresa.
        self.assertNotIn(self.ib.pk, self._ids({"search": "PP01-AZ"}))
        self.assertNotIn(self.ib.pk, self._ids({"search": f"LAB-{self.ib.pk:06d}"}))
        self.assertEqual(self._ids({"search": "PP01-AZ"}, user=self.superusuario_b), [self.ib.pk])
        self.assertEqual(self._ids({"search": f"LAB-{self.i1.pk:06d}"}, user=self.superusuario_b), [])

    # ── status ────────────────────────────────────────────────────────

    def test_status_uno_o_varios(self):
        self.assertEqual(self._ids({"status": "EXITO"}), [self.i4.pk, self.i1.pk])
        self.assertEqual(self._ids({"status": "pendiente,FALLIDO"}), [self.i3.pk, self.i2.pk])
        self.assertEqual(len(self._ids({"status": ""})), 4)

    def test_status_invalido_400(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        for valor in ("NADA", "EXITO,NADA", ","):
            with self.subTest(status=valor):
                resp = client.get(self.URL, {"status": valor})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(list(resp.json()), ["status"])

    # ── ordering ──────────────────────────────────────────────────────

    def test_orden_por_defecto_mas_reciente_primero(self):
        self.assertEqual(self._ids({}), [self.i4.pk, self.i3.pk, self.i2.pk, self.i1.pk])
        # Un campo fuera de la lista blanca se ignora (queda el orden por defecto).
        self.assertEqual(
            self._ids({"ordering": "zpl_enviado"}), [self.i4.pk, self.i3.pk, self.i2.pk, self.i1.pk]
        )

    def test_ordering_por_columnas(self):
        i1, i2, i3, i4 = self.i1.pk, self.i2.pk, self.i3.pk, self.i4.pk
        casos = {
            "folio": [i1, i2, i3, i4],
            "-folio": [i4, i3, i2, i1],
            "cantidad": [i2, i3, i1, i4],
            "-cantidad": [i4, i1, i3, i2],
            "created_at": [i1, i2, i3, i4],
            "-created_at": [i4, i3, i2, i1],
            "producto_nombre": [i2, i4, i1, i3],
            "status": [i1, i4, i2, i3],
            # Sin variante (NULL) al final en ambas direcciones; empate por id.
            "sku": [i1, i3, i2, i4],
            "-sku": [i3, i1, i4, i2],
            "producto_variante_nombre": [i1, i3, i2, i4],
            "-producto_variante_nombre": [i3, i1, i4, i2],
        }
        for ordering, esperado in casos.items():
            with self.subTest(ordering=ordering):
                self.assertEqual(self._ids({"ordering": ordering}), esperado)

    def test_ordering_con_el_desempate_del_front(self):
        """``useServerTable`` manda ``ordering=<campo>,-id`` (``tiebreaker: "-id"``):
        el ``-id`` se respeta en los empates, también en órdenes ascendentes."""
        from catalogo.models import ProductoVariante

        # i5 empata con i3 en cantidad y con i1/i3 en SKU.
        i5 = EtiquetaRFIDImpresion.objects.create(
            empresa=self.empresa, sucursal=self.sucursal, producto=self.polo,
            producto_variante=ProductoVariante.objects.get(pk=self.variante.pk),
            status=EtiquetaRFIDImpresion.Estatus.EXITO, cantidad=3, rfid_mode=False,
        )
        i1, i2, i3, i4, i5 = self.i1.pk, self.i2.pk, self.i3.pk, self.i4.pk, i5.pk
        casos = {
            "cantidad,-id": [i2, i5, i3, i1, i4],
            "-cantidad,-id": [i4, i1, i5, i3, i2],
            "sku,-id": [i5, i3, i1, i4, i2],
            "-sku,-id": [i5, i3, i1, i4, i2],
            "folio,-id": [i1, i2, i3, i4, i5],
            "-folio,-id": [i5, i4, i3, i2, i1],
        }
        for ordering, esperado in casos.items():
            with self.subTest(ordering=ordering):
                self.assertEqual(self._ids({"ordering": ordering}), esperado)
        # Con paginación, el empate de ``cantidad`` (i5, i3) no se parte ni se repite.
        primera = self._ids({"ordering": "cantidad,-id", "limit": 2, "offset": 0})
        segunda = self._ids({"ordering": "cantidad,-id", "limit": 2, "offset": 2})
        self.assertEqual(primera + segunda, [i2, i5, i3, i1])

    # ── combinados con paginación ─────────────────────────────────────

    def test_search_status_y_ordering_con_paginacion(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        with self.assertNumQueries(2):
            resp = client.get(
                self.URL, {"search": "lab", "ordering": "cantidad", "limit": 2, "offset": 2}
            )
        datos = resp.json()
        self.assertEqual(datos["count"], 4)
        self.assertEqual([f["id"] for f in datos["results"]], [self.i1.pk, self.i4.pk])
        self.assertNotIn("zpl_enviado", datos["results"][0])

        datos = client.get(
            self.URL, {"status": "EXITO", "ordering": "-cantidad", "limit": 1, "offset": 1}
        ).json()
        self.assertEqual(datos["count"], 2)
        self.assertEqual([f["id"] for f in datos["results"]], [self.i1.pk])

        datos = client.get(
            self.URL, {"search": "polo", "status": "PENDIENTE,EXITO", "ordering": "-folio", "limit": 10}
        ).json()
        self.assertEqual(datos["count"], 2)
        self.assertEqual([f["id"] for f in datos["results"]], [self.i3.pk, self.i1.pk])

    def test_detalle_ignora_los_parametros_del_listado(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        resp = client.get(
            f"{self.URL}{self.i1.pk}/", {"status": "FALLIDO", "search": "zzz", "ordering": "sku"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], self.i1.pk)
        self.assertEqual(client.get(f"{self.URL}{self.ib.pk}/", {"search": "PP01"}).status_code, 404)
