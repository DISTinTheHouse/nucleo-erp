"""Pruebas de ``hr``.

Ejecutar SIEMPRE con BD desechable (el ``.env`` del repo apunta a Supabase de
producción): un módulo de settings que haga ``from ERP.settings import *`` y
apunte ``DATABASES`` a ``django.db.backends.sqlite3`` / ``:memory:``.

    python manage.py test hr --settings=<ese_modulo>
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from hr.api.serializers import ContratoSerializer
from hr.models import Contrato, Empleado, Nomina, Puesto
from nucleo.models import Departamento, Empresa, Sucursal
from usuarios.models import Usuario

CONTRATOS_URL = "/api/v1/hr/contratos/"
MENSAJE_VIGENTE = "Este empleado ya tiene un contrato activo."


class HrBase(TestCase):
    @classmethod
    def _tenant(cls, codigo, email):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(empresa=empresa, codigo="MTY", nombre="Monterrey")
        departamento = Departamento.objects.create(
            empresa=empresa, sucursal=sucursal, codigo="PROD", nombre="Producción",
        )
        puesto = Puesto.objects.create(empresa=empresa, nombre="Operador")
        usuario = Usuario.objects.create(
            username=email, email=email, empresa=empresa, sucursal_default=sucursal,
        )
        return {
            "empresa": empresa,
            "sucursal": sucursal,
            "departamento": departamento,
            "puesto": puesto,
            "usuario": usuario,
        }

    @classmethod
    def _empleado(cls, tenant, numero):
        return Empleado.objects.create(
            empresa=tenant["empresa"],
            sucursal=tenant["sucursal"],
            departamento=tenant["departamento"],
            puesto=tenant["puesto"],
            numero_empleado=numero,
            nombre=f"Empleado {numero}",
            apellido_paterno="Pérez",
            fecha_ingreso=date(2025, 1, 6),
        )

    @classmethod
    def setUpTestData(cls):
        cls.a = cls._tenant("acme", "rh@acme.test")
        cls.b = cls._tenant("globex", "rh@globex.test")
        cls.empleado = cls._empleado(cls.a, "E-001")

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.a["usuario"])
        return client


class ContratoVigenteUnicoApiTests(HrBase):
    """Un empleado tiene a lo más un contrato vigente.

    "Vigente" es ``activo=True`` Y ``estado='activo'``: una baja lógica no
    cuenta, un ``terminado``/``renovado`` tampoco. Antes la regla contaba sólo
    por ``estado`` --una baja seguía bloqueando al reemplazo-- y un alta que
    omitía ``estado`` se saltaba la revisión y nacía activa con el default.
    """

    def _contrato(self, empleado=None, **kwargs):
        datos = {"fecha_inicio": date(2026, 1, 1), "salario": "12000.00"}
        datos.update(kwargs)
        return Contrato.objects.create(empleado=empleado or self.empleado, **datos)

    def _payload(self, empleado=None, **kwargs):
        datos = {
            "empleado": (empleado or self.empleado).pk,
            "tipo": "indefinido",
            "fecha_inicio": "2026-06-01",
            "salario": "15000.00",
        }
        datos.update(kwargs)
        return datos

    def _post(self, data, user=None):
        return self._client(user).post(CONTRATOS_URL, data, format="json")

    def _patch(self, contrato, data, user=None):
        return self._client(user).patch(f"{CONTRATOS_URL}{contrato.pk}/", data, format="json")

    def _vigentes(self, empleado=None):
        return Contrato.objects.filter(
            empleado=empleado or self.empleado, activo=True, estado="activo",
        ).count()

    def _assert_rechazo_por_vigente(self, resp):
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"estado": [MENSAJE_VIGENTE]})

    # -- Hueco 1: el alta que omite ``estado`` --------------------------------------

    def test_alta_sin_estado_no_crea_un_segundo_contrato_activo(self):
        self._contrato()
        payload = self._payload()
        self.assertNotIn("estado", payload)

        resp = self._post(payload)

        self._assert_rechazo_por_vigente(resp)
        self.assertEqual(self._vigentes(), 1)

    def test_alta_sin_estado_nace_activa_si_no_hay_otra_vigente(self):
        resp = self._post(self._payload())

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["estado"], "activo")
        self.assertEqual(self._vigentes(), 1)

    # -- Hueco 2: la baja lógica ---------------------------------------------------

    def test_un_contrato_dado_de_baja_no_bloquea_uno_nuevo(self):
        viejo = self._contrato()
        self.assertEqual(self._client().delete(f"{CONTRATOS_URL}{viejo.pk}/").status_code, 204)
        viejo.refresh_from_db()
        # La baja sólo toca ``activo``: ``estado`` se queda en 'activo'.
        self.assertEqual((viejo.activo, viejo.estado), (False, "activo"))

        resp = self._post(self._payload(estado="activo"))

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self._vigentes(), 1)
        self.assertEqual(Contrato.objects.filter(empleado=self.empleado).count(), 2)

    def test_un_contrato_terminado_o_renovado_no_bloquea_uno_nuevo(self):
        for estado in ("terminado", "renovado"):
            with self.subTest(estado=estado):
                empleado = self._empleado(self.a, f"E-{estado}")
                self._contrato(empleado=empleado, estado=estado)

                resp = self._post(self._payload(empleado=empleado))

                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertEqual(self._vigentes(empleado), 1)

    # -- La regla sigue en pie -------------------------------------------------------

    def test_dos_contratos_vigentes_siguen_rechazados(self):
        self._contrato()

        resp = self._post(self._payload(estado="activo"))

        self._assert_rechazo_por_vigente(resp)
        self.assertEqual(self._vigentes(), 1)

    def test_editar_un_contrato_vigente_no_choca_consigo_mismo(self):
        contrato = self._contrato()

        with self.subTest("PATCH de otro campo"):
            resp = self._patch(contrato, {"salario": "13000.00"})
            self.assertEqual(resp.status_code, 200, resp.content)

        with self.subTest("PATCH que reenvía estado y activo"):
            resp = self._patch(contrato, {"estado": "activo", "activo": True})
            self.assertEqual(resp.status_code, 200, resp.content)

        with self.subTest("PUT completo"):
            resp = self._client().put(
                f"{CONTRATOS_URL}{contrato.pk}/",
                self._payload(estado="activo", salario="14000.00"),
                format="json",
            )
            self.assertEqual(resp.status_code, 200, resp.content)

        contrato.refresh_from_db()
        self.assertEqual(str(contrato.salario), "14000.00")
        self.assertEqual(self._vigentes(), 1)

    def test_reactivar_una_baja_con_otro_vigente_devuelve_400(self):
        baja = self._contrato(activo=False)
        self._contrato(fecha_inicio=date(2026, 3, 1))

        resp = self._patch(baja, {"activo": True})

        self._assert_rechazo_por_vigente(resp)
        baja.refresh_from_db()
        self.assertFalse(baja.activo)

    def test_volver_a_activo_un_terminado_con_otro_vigente_devuelve_400(self):
        terminado = self._contrato(estado="terminado")
        self._contrato(fecha_inicio=date(2026, 3, 1))

        resp = self._patch(terminado, {"estado": "activo"})

        self._assert_rechazo_por_vigente(resp)
        terminado.refresh_from_db()
        self.assertEqual(terminado.estado, "terminado")

    def test_un_alta_que_no_queda_vigente_no_choca_con_la_vigente(self):
        # Capturar un contrato histórico no disputa la vigencia.
        self._contrato()

        for extra in ({"estado": "terminado"}, {"estado": "renovado"}, {"activo": False}):
            with self.subTest(**extra):
                resp = self._post(self._payload(**extra))
                self.assertEqual(resp.status_code, 201, resp.content)

        self.assertEqual(self._vigentes(), 1)

    def test_la_unicidad_es_por_empleado(self):
        self._contrato()
        otro = self._empleado(self.a, "E-002")

        resp = self._post(self._payload(empleado=otro))

        self.assertEqual(resp.status_code, 201, resp.content)

    def test_un_choque_en_la_base_se_reporta_como_400_y_no_como_500(self):
        # Carrera: la validación pasa, pero antes del INSERT otra petición deja
        # vigente un contrato para el mismo empleado. La constraint lo detiene y
        # el cliente recibe el mismo 400 que en la ruta normal.
        validate_original = ContratoSerializer.validate

        def validate_y_pierde_la_carrera(serializer, data):
            data = validate_original(serializer, data)
            self._contrato(fecha_inicio=date(2026, 5, 1))
            return data

        with patch.object(ContratoSerializer, "validate", validate_y_pierde_la_carrera):
            resp = self._post(self._payload())

        self._assert_rechazo_por_vigente(resp)
        self.assertEqual(Contrato.objects.filter(empleado=self.empleado).count(), 1)

    # -- Lo que no debe cambiar ------------------------------------------------------

    def test_fecha_fin_anterior_a_la_de_inicio_devuelve_400(self):
        resp = self._post(self._payload(fecha_inicio="2026-06-01", fecha_fin="2026-05-31"))

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json(), {"fecha_fin": ["La fecha de fin no puede ser anterior a la de inicio."]},
        )
        self.assertFalse(Contrato.objects.exists())

    def test_creado_por_lo_fija_el_servidor(self):
        resp = self._post(self._payload(creado_por=self.b["usuario"].pk))

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["creado_por"], self.a["usuario"].pk)

    def test_no_se_contrata_a_un_empleado_de_otra_empresa(self):
        ajeno = self._empleado(self.b, "E-900")

        resp = self._post(self._payload(empleado=ajeno))

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json(), {"empleado": ["El empleado no pertenece a la empresa del usuario."]},
        )
        self.assertFalse(Contrato.objects.filter(empleado=ajeno).exists())


class ContratoVigenteUnicoModeloTests(HrBase):
    """La regla vale fuera de la API: admin, shell, scripts."""

    def _nuevo(self, **kwargs):
        datos = {"empleado": self.empleado, "fecha_inicio": date(2026, 1, 1), "salario": "12000.00"}
        datos.update(kwargs)
        return Contrato(**datos)

    def test_la_base_rechaza_dos_vigentes_para_el_mismo_empleado(self):
        self._nuevo().save()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._nuevo(fecha_inicio=date(2026, 6, 1)).save()

    def test_la_base_admite_una_baja_o_un_terminado_junto_a_la_vigente(self):
        baja = self._nuevo()
        baja.save()
        baja.soft_delete()
        self._nuevo(estado="terminado").save()
        self._nuevo(estado="renovado").save()

        self._nuevo(fecha_inicio=date(2026, 6, 1)).save()

        self.assertEqual(
            Contrato.objects.filter(empleado=self.empleado, activo=True, estado="activo").count(), 1,
        )

    def test_reactivar_con_restore_choca_en_la_base_si_ya_hay_otra_vigente(self):
        # ``restore()`` guarda sin validar; la constraint es quien lo detiene.
        baja = self._nuevo()
        baja.save()
        baja.soft_delete()
        self._nuevo(fecha_inicio=date(2026, 6, 1)).save()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                baja.restore()

    def test_clean_aplica_la_misma_regla_que_la_api(self):
        vigente = self._nuevo()
        vigente.save()

        with self.subTest("un segundo vigente"):
            with self.assertRaises(DjangoValidationError) as ctx:
                self._nuevo().full_clean()
            self.assertEqual(ctx.exception.message_dict, {"estado": [MENSAJE_VIGENTE]})

        with self.subTest("no choca consigo mismo"):
            vigente.full_clean()

        with self.subTest("uno terminado"):
            self._nuevo(estado="terminado").full_clean()

        with self.subTest("uno dado de baja"):
            self._nuevo(activo=False).full_clean()

        vigente.soft_delete()
        with self.subTest("la baja ya no bloquea"):
            self._nuevo().full_clean()


class GenerarPeriodoSalarioTests(HrBase):
    """``generar_periodo`` toma el salario sólo de un contrato vigente.

    Contaba por ``estado='activo'`` sin mirar ``activo``: una baja lógica, que
    conserva ``estado='activo'``, seguía dando el salario. Sin contrato vigente
    se cae al ``salario_base`` del puesto, y sin éste no hay percepción.
    """

    URL = "/api/v1/hr/nominas/generar_periodo/"

    def setUp(self):
        Puesto.objects.filter(pk=self.a["puesto"].pk).update(salario_base=Decimal("9000.00"))

    def _contrato(self, salario, fecha_inicio=date(2026, 1, 1), **kwargs):
        return Contrato.objects.create(
            empleado=self.empleado, fecha_inicio=fecha_inicio, salario=salario, **kwargs,
        )

    def _generar(self):
        resp = self._client().post(
            self.URL, {"periodo_inicio": "2026-07-01", "periodo_fin": "2026-07-15"}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return Nomina.objects.get(pk__in=resp.json()["ids"], empleado=self.empleado)

    def _percepcion(self, nomina):
        return list(nomina.detalles.filter(codigo="PER001").values_list("monto", flat=True))

    def test_con_contrato_vigente_usa_su_salario(self):
        self._contrato("12000.00")

        nomina = self._generar()

        self.assertEqual(nomina.salario_base, Decimal("12000.00"))
        self.assertEqual(self._percepcion(nomina), [Decimal("6000.00")])

    def test_una_baja_sin_reemplazo_cae_al_salario_del_puesto(self):
        self._contrato("12000.00").soft_delete()

        nomina = self._generar()

        self.assertEqual(nomina.salario_base, Decimal("9000.00"))
        self.assertEqual(self._percepcion(nomina), [Decimal("4500.00")])

    def test_una_baja_sin_reemplazo_ni_salario_de_puesto_no_genera_percepcion(self):
        Puesto.objects.filter(pk=self.a["puesto"].pk).update(salario_base=None)
        self._contrato("12000.00").soft_delete()

        nomina = self._generar()

        self.assertIsNone(nomina.salario_base)
        self.assertEqual(self._percepcion(nomina), [])

    def test_una_baja_con_reemplazo_vigente_usa_el_del_reemplazo(self):
        # La baja es la más reciente: ordenar por ``fecha_inicio`` la elegía. Nace
        # ya dada de baja --``activo=False``, ``estado='activo'``, lo mismo que
        # deja ``soft_delete()``-- porque la constraint no admite dos vigentes.
        self._contrato("15000.00", fecha_inicio=date(2026, 1, 1))
        self._contrato("20000.00", fecha_inicio=date(2026, 6, 1), activo=False)

        nomina = self._generar()

        self.assertEqual(nomina.salario_base, Decimal("15000.00"))
        self.assertEqual(self._percepcion(nomina), [Decimal("7500.00")])


MENSAJE_EMPLEADO_INACTIVO = "No se puede dejar vigente un contrato de un empleado inactivo."


class ContratoEmpleadoInactivoApiTests(HrBase):
    """Un contrato no puede QUEDAR vigente si su empleado está inactivo.

    Sólo se rechaza lo que quedaría vigente (``activo=True`` Y
    ``estado='activo'``, con los valores finales): capturar o editar contratos
    históricos de un empleado dado de baja sigue permitido.
    """

    def setUp(self):
        self.inactivo = self._empleado(self.a, "E-INA")
        self.inactivo.soft_delete()
        self.inactivo.refresh_from_db()
        self.assertFalse(self.inactivo.activo)

    def _contrato(self, empleado, **kwargs):
        datos = {"fecha_inicio": date(2026, 1, 1), "salario": "12000.00"}
        datos.update(kwargs)
        return Contrato.objects.create(empleado=empleado, **datos)

    def _payload(self, empleado, **kwargs):
        datos = {
            "empleado": empleado.pk,
            "tipo": "indefinido",
            "fecha_inicio": "2026-06-01",
            "salario": "15000.00",
        }
        datos.update(kwargs)
        return datos

    def _post(self, data):
        return self._client().post(CONTRATOS_URL, data, format="json")

    def _patch(self, contrato, data):
        return self._client().patch(f"{CONTRATOS_URL}{contrato.pk}/", data, format="json")

    def _assert_rechazo_por_inactivo(self, resp):
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"empleado": [MENSAJE_EMPLEADO_INACTIVO]})

    def test_alta_vigente_para_un_empleado_inactivo_devuelve_400(self):
        for extra in ({}, {"estado": "activo"}):
            with self.subTest(**extra):
                resp = self._post(self._payload(self.inactivo, **extra))

                self._assert_rechazo_por_inactivo(resp)
        self.assertFalse(Contrato.objects.filter(empleado=self.inactivo).exists())

    def test_reactivar_una_baja_de_un_empleado_inactivo_devuelve_400(self):
        baja = self._contrato(self.inactivo, activo=False)

        resp = self._patch(baja, {"activo": True})

        self._assert_rechazo_por_inactivo(resp)
        baja.refresh_from_db()
        self.assertFalse(baja.activo)

    def test_volver_a_activo_un_terminado_de_un_empleado_inactivo_devuelve_400(self):
        terminado = self._contrato(self.inactivo, estado="terminado")

        resp = self._patch(terminado, {"estado": "activo"})

        self._assert_rechazo_por_inactivo(resp)
        terminado.refresh_from_db()
        self.assertEqual(terminado.estado, "terminado")

    def test_mover_un_contrato_vigente_a_un_empleado_inactivo_devuelve_400(self):
        # El ``empleado`` final es el que viene en la petición.
        vigente = self._contrato(self.empleado)

        resp = self._patch(vigente, {"empleado": self.inactivo.pk})

        self._assert_rechazo_por_inactivo(resp)
        vigente.refresh_from_db()
        self.assertEqual(vigente.empleado_id, self.empleado.pk)

    def test_editar_un_contrato_no_vigente_de_un_empleado_inactivo_se_permite(self):
        terminado = self._contrato(self.inactivo, estado="terminado")
        baja = self._contrato(self.inactivo, activo=False, fecha_inicio=date(2026, 3, 1))

        for contrato in (terminado, baja):
            with self.subTest(contrato=contrato.pk):
                resp = self._patch(contrato, {"observaciones": "Finiquito entregado."})

                self.assertEqual(resp.status_code, 200, resp.content)
                contrato.refresh_from_db()
                self.assertEqual(contrato.observaciones, "Finiquito entregado.")

    def test_alta_historica_para_un_empleado_inactivo_se_permite(self):
        for extra in ({"estado": "terminado"}, {"estado": "renovado"}, {"activo": False}):
            with self.subTest(**extra):
                resp = self._post(self._payload(self.inactivo, **extra))

                self.assertEqual(resp.status_code, 201, resp.content)

    def test_un_vigente_que_quedo_de_un_empleado_inactivo_se_puede_terminar(self):
        # Contrato vigente de un empleado que se dio de baja después (la baja del
        # empleado no toca sus contratos). Editarlo sin terminarlo lo dejaría
        # vigente y se rechaza; terminarlo es la salida.
        empleado = self._empleado(self.a, "E-BAJA")
        vigente = self._contrato(empleado)
        empleado.soft_delete()

        with self.subTest("sin terminarlo"):
            resp = self._patch(vigente, {"observaciones": "Nota"})
            self._assert_rechazo_por_inactivo(resp)

        with self.subTest("terminándolo"):
            resp = self._patch(vigente, {"estado": "terminado", "fecha_fin": "2026-08-31"})
            self.assertEqual(resp.status_code, 200, resp.content)

        vigente.refresh_from_db()
        self.assertEqual(vigente.estado, "terminado")

    def test_con_un_empleado_activo_nada_cambia(self):
        resp = self._post(self._payload(self.empleado))
        self.assertEqual(resp.status_code, 201, resp.content)

        contrato = Contrato.objects.get(pk=resp.json()["id"])
        resp = self._patch(contrato, {"observaciones": "Sin cambios", "activo": True, "estado": "activo"})

        self.assertEqual(resp.status_code, 200, resp.content)


class ContratoEmpleadoInactivoModeloTests(HrBase):
    """``clean()`` aplica la misma regla: cubre el admin (``full_clean()``)."""

    def test_full_clean_rechaza_un_vigente_de_un_empleado_inactivo(self):
        inactivo = self._empleado(self.a, "E-INA")
        inactivo.soft_delete()
        base = {"empleado": inactivo, "fecha_inicio": date(2026, 1, 1), "salario": "12000.00"}

        with self.subTest("vigente"):
            with self.assertRaises(DjangoValidationError) as ctx:
                Contrato(**base).full_clean()
            self.assertEqual(ctx.exception.message_dict, {"empleado": [MENSAJE_EMPLEADO_INACTIVO]})

        with self.subTest("terminado"):
            Contrato(estado="terminado", **base).full_clean()

        with self.subTest("dado de baja"):
            Contrato(activo=False, **base).full_clean()

        with self.subTest("empleado activo"):
            Contrato(**{**base, "empleado": self.empleado}).full_clean()
