"""Pruebas del manejo de colisiones de EPC en etiquetas RFID.

Cubren las dos capas de defensa sobre ``EtiquetaRFIDDetalle.epc`` (``unique=True``
global): el pre-chequeo del serializer (400) y la red de seguridad del service
(409), más el bucle acotado de regeneración para EPC generados por backend.
"""

from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.transaction import TransactionManagementError
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from catalogo.models import Producto
from nucleo.models import Empresa, Sucursal
from usuarios.models import Usuario
from wms.api.serializers import EtiquetaRFIDCreateSerializer
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion
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

        self.assertIn("^RFW,E,,N", zpl)
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
