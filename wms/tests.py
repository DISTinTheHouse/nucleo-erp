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

from catalogo.models import Producto
from nucleo.models import Empresa, Sucursal
from usuarios.models import Usuario
from wms.api.serializers import EtiquetaRFIDCreateSerializer
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion, LectorRFID, RfidScan
from wms.services.rfid_label_service import (
    MAX_INTENTOS_EPC,
    EtiquetaRFIDColision409,
    RFIDLabelService,
)

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
        """``^RFW,E`` debe llevar el EPC íntegro, sin recortes."""
        epc = RFIDLabelService._generate_epc_list(1, producto=self.producto)[0]["epc"]

        zpl = RFIDLabelService._build_zpl_rfid(epc, producto=self.producto)

        self.assertIn("^RFW,E", zpl)
        self.assertIn(f"^FD{epc}^FS", zpl)


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
        cls.superuser = Usuario.objects.create(
            username="scan-root", email="scan-root@example.com", is_superuser=True
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

    def test_lector_sin_sucursal_no_valida(self):
        with self.assertRaises(DjangoValidationError):
            LectorRFID(empresa=self.empresa_a, nombre="sin suc").full_clean()

    def test_lector_con_sucursal_de_otra_empresa_no_valida(self):
        lector = LectorRFID(empresa=self.empresa_a, sucursal=self.sucursal_b, nombre="mal")
        with self.assertRaises(DjangoValidationError):
            lector.full_clean()

    def test_scans_superuser_ve_todas(self):
        self._sembrar(self.empresa_a, self.lector_a, 2, "aaaa")
        self._sembrar(self.empresa_b, self.lector_b, 2, "bbbb")
        resp = self._api(self.superuser).get(self.SCANS_URL)
        self.assertEqual(len(resp.json()["scans"]), 4)

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

    def test_picking_crea_transferencia_con_folio_tr_y_mueve_stock(self):
        from wms.services.picking_service import PickingService

        picking, _ = PickingService.handle_store(self._data(self.Decimal("3")), self.usuario)

        self.assertIsNotNone(picking.transferencia_id)
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

    def test_picking_con_lineas_surtidas_no_se_traslada(self):
        from ventas.models import PedidoDetalle
        from wms.models import PickingDetalle
        PickingDetalle.objects.create(
            picking=self.picking,
            pedido_detalle=PedidoDetalle.objects.create(
                pedido=self.picking.pedido, producto_nombre_externo="Muestra"
            ),
            cantidad_solicitada=2, cantidad_asignada=2, cantidad_surtida=1,
        )
        self.assertEqual(self._post(self.apartados).status_code, 400)
        self.picking.refresh_from_db()
        self.assertEqual(self.picking.almacen_id, self.bordados.pk)

    def test_picking_completado_no_se_traslada(self):
        from wms.models import Picking
        Picking.objects.filter(pk=self.picking.pk).update(estado=Picking.Estado.COMPLETADO)
        self.assertEqual(self._post(self.apartados).status_code, 400)

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

        Los pickings van sin TR: ``transferencia_folio`` aún consulta la TR por fila (#698).
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
    """EC-574: empacar confirma el surtido (WM-01, opción B) y, sin embarque
    parcial, despachar exige todo el pedido empacado (WM-02, regla O1).

    Todo por la API sobre un picking creado con ``PickingService`` (surtida 0):
    ninguna prueba fija ``cantidad_surtida`` a mano.
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
        producto = Producto.objects.create(empresa=cls.empresa, nombre="Playera", codigo="PL574")
        Existencia.objects.create(producto=producto, almacen=cls.origen, stock=20, cantidad=Decimal("20"))
        cls.pedido = Pedido.objects.create(
            empresa=cls.empresa, sucursal=cls.sucursal,
            cliente=Cliente.objects.create(empresa=cls.empresa, nombre="Cliente"),
            moneda=Moneda.objects.create(codigo_iso="MXN", nombre="Peso"), folio="P-574",
            persona_pagos="Pagos", correo_facturas="ec574@example.com", telefono_pagos="8100000000",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
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
        return self.api.post(
            "/api/v1/wms/despachos/", {"packing": packing["id"], "despacho_detalle": lineas}, format="json"
        )

    def _mensaje(self, resp, campo):
        cuerpo = resp.json()[campo]
        return cuerpo if isinstance(cuerpo, str) else cuerpo[0]

    # --- WM-01 -------------------------------------------------------------
    def test_picking_nuevo_arranca_sin_surtido(self):
        self.assertEqual((self.linea_m.cantidad_surtida, self.linea_g.cantidad_surtida), (0, 0))

    def test_empacar_confirma_el_surtido_y_audita(self):
        from auditoria.models import AuditoriaEvento
        from wms.models import Picking, PickingDetalle

        packing = self._empacar_ok((self.linea_m, 3))

        self.linea_m.refresh_from_db()
        self.linea_g.refresh_from_db()
        self.picking.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, Decimal("3"))
        self.assertIsNotNone(self.linea_m.fecha_surtido)
        self.assertEqual(self.linea_m.estado, PickingDetalle.EstadoLinea.PARCIAL)
        self.assertEqual(self.linea_g.cantidad_surtida, 0)
        self.assertEqual(self.linea_g.estado, PickingDetalle.EstadoLinea.PENDIENTE)
        self.assertEqual(self.picking.estado, Picking.Estado.PARCIAL)
        self.assertEqual(self.picking.total_lineas_completas, 0)
        self.assertIsNone(self.picking.fecha_fin)

        evento = AuditoriaEvento.objects.get(accion="SURTIDO_POR_PACKING")
        self.assertEqual(
            (evento.modulo, evento.tabla, evento.id_registro, evento.usuario_id, evento.empresa_id),
            ("wms", "picking", str(self.picking.pk), self.usuario.pk, self.empresa.pk),
        )
        clave = str(self.linea_m.pk)
        self.assertEqual(set(evento.antes_json["lineas"]), {clave})
        self.assertEqual(evento.antes_json["estado"], Picking.Estado.PENDIENTE)
        self.assertEqual(Decimal(evento.antes_json["lineas"][clave]["cantidad_surtida"]), 0)
        self.assertEqual(evento.antes_json["lineas"][clave]["estado"], "PENDIENTE")
        self.assertEqual(evento.despues_json["estado"], Picking.Estado.PARCIAL)
        self.assertEqual(Decimal(evento.despues_json["lineas"][clave]["cantidad_surtida"]), Decimal("3"))
        self.assertEqual(evento.despues_json["lineas"][clave]["estado"], "PARCIAL")
        self.assertEqual(
            (evento.despues_json["packing_id"], evento.despues_json["packing_folio"]),
            (packing["id"], packing["folio"]),
        )

    def test_segundo_packing_completa_el_picking(self):
        from auditoria.models import AuditoriaEvento
        from wms.models import Picking, PickingDetalle

        self._empacar_ok((self.linea_m, 3))
        self._empacar_ok((self.linea_m, 1), (self.linea_g, 2))

        self.picking.refresh_from_db()
        for linea, surtida in ((self.linea_m, 4), (self.linea_g, 2)):
            linea.refresh_from_db()
            self.assertEqual(linea.cantidad_surtida, Decimal(surtida))
            self.assertEqual(linea.estado, PickingDetalle.EstadoLinea.SURTIDA)
        self.assertEqual(self.picking.estado, Picking.Estado.COMPLETADO)
        self.assertEqual(self.picking.total_lineas_completas, 2)
        self.assertIsNotNone(self.picking.fecha_fin)
        self.assertEqual(AuditoriaEvento.objects.filter(accion="SURTIDO_POR_PACKING").count(), 2)

    def test_empacar_mas_de_lo_asignado_es_400(self):
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
    def test_sin_embarque_parcial_despacha_solo_con_todo_empacado(self):
        primero = self._empacar_ok((self.linea_m, 4))
        resp = self._despachar(primero)
        self.assertEqual(resp.status_code, 400)
        mensaje = self._mensaje(resp, "despacho_detalle")
        self.assertEqual(
            mensaje, "El pedido no permite embarque parcial: faltan 2 piezas por empacar de las 6 del pedido."
        )
        self.assertIsNone(self.STALE_DESPACHO_RE.search(mensaje))
        self.assertIsNone(self.STALE_PACKING_RE.search(mensaje))

        segundo = self._empacar_ok((self.linea_g, 2))
        self.assertEqual(self._despachar(primero).status_code, 201)
        self.assertEqual(self._despachar(segundo).status_code, 201)

    def test_con_embarque_parcial_despacha_parcial(self):
        from ventas.models import Pedido

        Pedido.objects.filter(pk=self.pedido.pk).update(embarque_parcial=True)
        self.assertEqual(self._despachar(self._empacar_ok((self.linea_m, 1))).status_code, 201)

    def test_packing_o_linea_cancelados_no_cuentan(self):
        from wms.models import Packing, PackingDetalle

        primero = self._empacar_ok((self.linea_m, 4))
        segundo = self._empacar_ok((self.linea_g, 2))
        Packing.objects.filter(pk=segundo["id"]).update(estado="CANCELADO")
        self.assertEqual(self._despachar(primero).status_code, 400)

        Packing.objects.filter(pk=segundo["id"]).update(estado="PENDIENTE")
        PackingDetalle.objects.filter(packing_id=segundo["id"]).update(estado="CANCELADO")
        resp = self._despachar(primero)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("faltan 2 piezas", self._mensaje(resp, "despacho_detalle"))

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
        resp = self._despachar(primero)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("faltan 2 piezas por empacar de las 6", self._mensaje(resp, "despacho_detalle"))

    # --- revisión de código ------------------------------------------------
    def test_despacho_de_parte_de_un_packing(self):
        from ventas.models import Pedido

        packing = self._empacar_ok((self.linea_m, 4), (self.linea_g, 2))
        solo_m = {**packing, "packing_detalle": packing["packing_detalle"][:1]}
        resp = self._despachar(solo_m)
        self.assertEqual(resp.status_code, 400)
        mensaje = self._mensaje(resp, "despacho_detalle")
        self.assertIn("todas las líneas del packing", mensaje)
        self.assertIsNone(self.STALE_DESPACHO_RE.search(mensaje))
        self.assertIsNone(self.STALE_PACKING_RE.search(mensaje))

        Pedido.objects.filter(pk=self.pedido.pk).update(embarque_parcial=True)
        self.assertEqual(self._despachar(solo_m).status_code, 201)

    def test_reempacar_tras_cancelar_no_infla_el_surtido(self):
        from wms.models import Packing, PickingDetalle

        primero = self._empacar_ok((self.linea_m, 4))
        Packing.objects.filter(pk=primero["id"]).update(estado="CANCELADO")
        self._empacar_ok((self.linea_m, 4))
        self.linea_m.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, Decimal("4"))
        self.assertEqual(self.linea_m.estado, PickingDetalle.EstadoLinea.SURTIDA)

    def test_linea_empacada_antes_del_cambio_llega_a_surtida(self):
        from wms.models import Packing, PackingDetalle, PickingDetalle

        # Packing previo a EC-574: dejó 3 piezas empacadas sin tocar el surtido.
        previo = Packing.objects.create(
            folio="PA-PREVIO", empresa=self.empresa, sucursal=self.sucursal, pedido=self.pedido,
            picking=self.picking, operador=self.usuario, usuario=self.usuario,
        )
        PackingDetalle.objects.create(packing=previo, picking_detalle=self.linea_m, cantidad_empacada=3)
        self.linea_m.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, 0)

        self._empacar_ok((self.linea_m, 1))
        self.linea_m.refresh_from_db()
        self.assertEqual(self.linea_m.cantidad_surtida, Decimal("4"))
        self.assertEqual(self.linea_m.estado, PickingDetalle.EstadoLinea.SURTIDA)
        self.linea_g.refresh_from_db()
        self.assertEqual(self.linea_g.cantidad_surtida, 0)  # no tocada: no se recalcula

    def test_linea_de_picking_cancelada_no_cuenta_en_o1(self):
        from wms.models import PickingDetalle

        packing = self._empacar_ok((self.linea_m, 4), (self.linea_g, 2))
        PickingDetalle.objects.filter(pk=self.linea_g.pk).update(estado=PickingDetalle.EstadoLinea.CANCELADA)
        resp = self._despachar(packing)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("faltan 2 piezas", self._mensaje(resp, "despacho_detalle"))

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

    def test_onboarding_de_despacho_explica_o1(self):
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
                "despachable": False,
                "motivo": "El pedido no permite embarque parcial: faltan 2 piezas por empacar de las 6 del pedido.",
            },
        )
        self.assertFalse(any(d["disponible_para_despacho"] for d in datos["despacho_detalle"]))
        self.assertTrue(all(not d["ya_despachado"] for d in datos["despacho_detalle"]))

        self._empacar_ok((self.linea_g, 2))
        datos = onboarding(primero)
        self.assertTrue(datos["embarque"]["despachable"])
        self.assertIsNone(datos["embarque"]["motivo"])
        self.assertEqual(datos["embarque"]["piezas_por_empacar"], "0")
        self.assertTrue(all(d["disponible_para_despacho"] for d in datos["despacho_detalle"]))
