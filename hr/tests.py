"""Pruebas de ``hr``.

Ejecutar SIEMPRE con BD desechable (el ``.env`` del repo apunta a Supabase de
producción): un módulo de settings que haga ``from ERP.settings import *`` y
apunte ``DATABASES`` a ``django.db.backends.sqlite3`` / ``:memory:``.

    python manage.py test hr --settings=<ese_modulo>
"""

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from hr.api.serializers import AsistenciaSerializer, ContratoSerializer
from hr.models import Asistencia, Contrato, Empleado, Nomina, Puesto, Turno
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


ASISTENCIAS_URL = "/api/v1/hr/asistencias/"
ENTRADA_URL = f"{ASISTENCIAS_URL}registrar_entrada/"
SALIDA_URL = f"{ASISTENCIAS_URL}registrar_salida/"
DIA = date(2026, 9, 28)
MENSAJE_ENTRADA_DUPLICADA = (
    "La entrada de este empleado para esta fecha ya está registrada. "
    "Para corregirla, edita el registro de asistencia."
)
MENSAJE_SALIDA_DUPLICADA = (
    "La salida de este empleado para esta fecha ya está registrada. "
    "Para corregirla, edita el registro de asistencia."
)


def _mx(dia, hora, minuto=0):
    """Datetime aware en la zona del proyecto (``America/Mexico_City``)."""
    return timezone.make_aware(datetime.combine(dia, time(hora, minuto)))


class AsistenciaBase(HrBase):
    """Turno de 08:00 a 17:00, 5 minutos de tolerancia y 8 horas base."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.turno = Turno.objects.create(
            empresa=cls.a["empresa"], nombre="Matutino",
            hora_entrada=time(8, 0), hora_salida=time(17, 0),
            tolerancia_retardo_minutos=5, horas_base_diarias=Decimal("8.00"),
        )
        Empleado.objects.filter(pk=cls.empleado.pk).update(turno=cls.turno)
        cls.empleado.refresh_from_db()

    def _asistencia(self, **kwargs):
        datos = {"empleado": self.empleado, "turno": self.turno, "fecha": DIA}
        datos.update(kwargs)
        return Asistencia.objects.create(**datos)

    def _post(self, data):
        return self._client().post(ASISTENCIAS_URL, data, format="json")

    def _patch(self, asistencia, data, user=None):
        return self._client(user).patch(f"{ASISTENCIAS_URL}{asistencia.pk}/", data, format="json")

    def _payload(self, **kwargs):
        datos = {"empleado": self.empleado.pk, "turno": self.turno.pk, "fecha": DIA.isoformat()}
        datos.update(kwargs)
        return datos


class AsistenciaEstadoDerivadoTests(AsistenciaBase):
    """El servidor deriva ``estado``: sin entrada es ``falta``; con entrada,
    ``retardo`` si pasa la tolerancia y ``puntual`` si no. Solo ``justificada``
    se fija a mano."""

    def test_entrada_dentro_de_la_tolerancia_es_puntual(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 5))

        self.assertEqual((asistencia.estado, asistencia.minutos_retardo), ("puntual", 0))

    def test_entrada_fuera_de_la_tolerancia_es_retardo(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 12))

        # 12 minutos tarde menos 5 de tolerancia.
        self.assertEqual((asistencia.estado, asistencia.minutos_retardo), ("retardo", 7))

    def test_corregir_la_entrada_revierte_el_retardo(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 30))
        self.assertEqual(asistencia.estado, "retardo")

        resp = self._patch(asistencia, {"hora_entrada": _mx(DIA, 7, 58).isoformat()})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["estado"], resp.json()["minutos_retardo"]), ("puntual", 0))

    def test_sin_entrada_es_falta(self):
        resp = self._post(self._payload())

        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["estado"], "falta")
        self.assertEqual(body["minutos_retardo"], 0)
        self.assertIsNone(body["horas_normales"])
        self.assertIsNone(body["horas_extra"])

    def test_quitar_la_entrada_la_vuelve_falta(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 30))

        resp = self._patch(asistencia, {"hora_entrada": None})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["estado"], resp.json()["minutos_retardo"]), ("falta", 0))

    def test_un_estado_distinto_de_justificada_se_reemplaza_por_el_derivado(self):
        for enviado in ("falta", "puntual"):
            with self.subTest(enviado=enviado):
                resp = self._post(self._payload(
                    empleado=self._empleado(self.a, f"E-{enviado}").pk,
                    estado=enviado, hora_entrada=_mx(DIA, 8, 30).isoformat(),
                ))

                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertEqual(resp.json()["estado"], "retardo")

    def test_justificada_se_conserva_y_se_quita_al_mandar_otro_estado(self):
        asistencia = self._asistencia()
        self.assertEqual(asistencia.estado, "falta")

        with self.subTest("se fija"):
            resp = self._patch(asistencia, {"estado": "justificada"})
            self.assertEqual(resp.json()["estado"], "justificada")

        with self.subTest("sobrevive a un cambio de horas"):
            resp = self._patch(asistencia, {"hora_entrada": _mx(DIA, 8, 20).isoformat()})
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertEqual(resp.json()["estado"], "justificada")
            # Los campos calculados se siguen calculando.
            self.assertEqual(resp.json()["minutos_retardo"], 15)

        with self.subTest("se quita"):
            resp = self._patch(asistencia, {"estado": "puntual"})
            self.assertEqual(resp.json()["estado"], "retardo")

    def test_horas_con_y_sin_salida(self):
        con_salida = self._asistencia(hora_entrada=_mx(DIA, 8, 0), hora_salida=_mx(DIA, 18, 0))
        sin_salida = self._asistencia(
            empleado=self._empleado(self.a, "E-002"), hora_entrada=_mx(DIA, 8, 0),
        )

        self.assertEqual((con_salida.horas_normales, con_salida.horas_extra), (Decimal("8.00"), Decimal("2.00")))
        self.assertEqual((sin_salida.horas_normales, sin_salida.horas_extra), (None, None))

    def test_los_campos_calculados_se_ignoran_en_la_entrada(self):
        resp = self._post(self._payload(
            hora_entrada=_mx(DIA, 8, 0).isoformat(), hora_salida=_mx(DIA, 17, 0).isoformat(),
            minutos_retardo=99, minutos_tolerancia=60, horas_normales="1.00", horas_extra="50.00",
        ))

        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["minutos_retardo"], 0)
        self.assertEqual(body["minutos_tolerancia"], 5)
        self.assertEqual((body["horas_normales"], body["horas_extra"]), ("8.00", "1.00"))


class AsistenciaValidacionTests(AsistenciaBase):
    def test_patch_de_la_entrada_se_compara_con_la_salida_guardada(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0), hora_salida=_mx(DIA, 12, 0))

        resp = self._patch(asistencia, {"hora_entrada": _mx(DIA, 13, 0).isoformat()})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json(), {"hora_salida": ["La hora de salida debe ser posterior a la de entrada."]},
        )
        asistencia.refresh_from_db()
        self.assertEqual(asistencia.hora_entrada, _mx(DIA, 8, 0))

    def test_una_salida_sin_entrada_se_rechaza(self):
        with self.subTest("alta"):
            resp = self._post(self._payload(hora_salida=_mx(DIA, 17, 0).isoformat()))
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(
                resp.json(), {"hora_salida": ["No se puede registrar la salida sin una hora de entrada."]},
            )

        with self.subTest("patch sobre un registro sin entrada"):
            asistencia = self._asistencia()
            resp = self._patch(asistencia, {"hora_salida": _mx(DIA, 17, 0).isoformat()})
            self.assertEqual(resp.status_code, 400, resp.content)

    def test_una_hora_de_otro_dia_se_rechaza(self):
        with self.subTest("entrada"):
            resp = self._post(self._payload(hora_entrada=_mx(date(2026, 9, 27), 8, 0).isoformat()))
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(
                resp.json(),
                {"hora_entrada": ["La hora de entrada debe corresponder a la fecha de la asistencia."]},
            )

        with self.subTest("salida"):
            # Es también lo que causaba el desbordamiento de Decimal(4,2) en
            # Postgres: una salida días después de la entrada.
            resp = self._post(self._payload(
                hora_entrada=_mx(DIA, 8, 0).isoformat(),
                hora_salida=_mx(date(2026, 10, 3), 17, 0).isoformat(),
            ))
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(
                resp.json(),
                {"hora_salida": ["La hora de salida debe corresponder a la fecha de la asistencia."]},
            )

        with self.subTest("patch que solo cambia la fecha"):
            asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0))
            resp = self._patch(asistencia, {"fecha": "2026-09-29"})
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertIn("hora_entrada", resp.json())

    def test_la_fecha_local_es_la_de_mexico_no_la_de_utc(self):
        # 23:30 del 28 en México es 05:30 del 29 en UTC: sigue siendo el 28.
        resp = self._post(self._payload(
            hora_entrada=_mx(DIA, 8, 0).isoformat(),
            hora_salida="2026-09-29T05:30:00Z",
        ))

        self.assertEqual(resp.status_code, 201, resp.content)


class AsistenciaChecadorTests(AsistenciaBase):
    def _entrada(self, **data):
        cuerpo = {"empleado_id": self.empleado.pk, "fecha": DIA.isoformat(), "hora": "2026-09-28 08:12:00"}
        cuerpo.update(data)
        return self._client().post(ENTRADA_URL, cuerpo, format="json")

    def _salida(self, **data):
        cuerpo = {"empleado_id": self.empleado.pk, "fecha": DIA.isoformat(), "hora": "2026-09-28 17:30:00"}
        cuerpo.update(data)
        return self._client().post(SALIDA_URL, cuerpo, format="json")

    # -- registrar_entrada -----------------------------------------------------------

    def test_registrar_entrada_crea_el_registro(self):
        resp = self._entrada()

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["estado"], body["minutos_retardo"], body["turno"]), ("retardo", 7, self.turno.pk))

    def test_una_segunda_entrada_responde_409_sin_tocar_el_registro(self):
        self.assertEqual(self._entrada().status_code, 200)

        resp = self._entrada(hora="2026-09-28 07:55:00")

        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json(), {"detail": MENSAJE_ENTRADA_DUPLICADA})
        asistencia = Asistencia.objects.get(empleado=self.empleado, fecha=DIA)
        self.assertEqual(asistencia.hora_entrada, _mx(DIA, 8, 12))

    def test_la_entrada_se_pone_en_un_registro_existente_sin_entrada(self):
        falta = self._asistencia()
        self.assertEqual(falta.estado, "falta")

        resp = self._entrada(hora="2026-09-28 08:00:00")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["id"], falta.pk)
        self.assertEqual(resp.json()["estado"], "puntual")
        self.assertEqual(Asistencia.objects.filter(empleado=self.empleado).count(), 1)

    def test_una_creacion_concurrente_responde_409_y_no_500(self):
        # Simula la carrera: la revisión previa no ve el registro que otra
        # petición ya creó, y el INSERT choca con la constraint. SQLite sí
        # aplica la constraint única, así que se prueba la traducción; el
        # bloqueo con select_for_update solo se puede comprobar en Postgres.
        self._asistencia(hora_entrada=_mx(DIA, 8, 0))

        with patch("hr.api.views.AsistenciaViewSet._asistencia_del_dia", return_value=None):
            resp = self._entrada()

        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json(), {"detail": MENSAJE_ENTRADA_DUPLICADA})

    # -- registrar_salida ------------------------------------------------------------

    def test_registrar_salida_calcula_las_horas(self):
        self._entrada(hora="2026-09-28 08:00:00")

        resp = self._salida(hora="2026-09-28 18:00:00")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["horas_normales"], resp.json()["horas_extra"]), ("8.00", "2.00"))

    def test_una_segunda_salida_responde_409_sin_tocar_el_registro(self):
        self._entrada()
        self.assertEqual(self._salida().status_code, 200)

        resp = self._salida(hora="2026-09-28 19:00:00")

        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json(), {"detail": MENSAJE_SALIDA_DUPLICADA})
        asistencia = Asistencia.objects.get(empleado=self.empleado, fecha=DIA)
        self.assertEqual(asistencia.hora_salida, _mx(DIA, 17, 30))

    def test_salida_sin_registro_sigue_respondiendo_404(self):
        resp = self._salida()

        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.json(), {"detail": "No se encontró registro de entrada para esta fecha."})

    def test_salida_sobre_un_registro_sin_entrada_responde_400(self):
        self._asistencia()

        resp = self._salida()

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json(), {"detail": "No se puede registrar la salida: el registro no tiene hora de entrada."},
        )

    def test_salida_que_no_es_posterior_a_la_entrada_responde_400(self):
        self._entrada()

        for hora in ("2026-09-28 08:12:00", "2026-09-28 08:00:00"):
            with self.subTest(hora=hora):
                resp = self._salida(hora=hora)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(
                    resp.json(), {"detail": "La hora de salida debe ser posterior a la hora de entrada."},
                )
        self.assertIsNone(Asistencia.objects.get(empleado=self.empleado, fecha=DIA).hora_salida)

    # -- Validación común de las dos acciones ----------------------------------------

    def test_empleado_id_no_numerico_responde_400(self):
        for accion in (self._entrada, self._salida):
            for valor in ("abc", "3.5", True):
                with self.subTest(accion=accion.__name__, valor=valor):
                    resp = accion(empleado_id=valor)
                    self.assertEqual(resp.status_code, 400, resp.content)
                    self.assertIn("empleado_id", resp.json())

    def test_una_hora_de_otro_dia_responde_400_en_las_dos_acciones(self):
        esperado = {"detail": "La fecha de la hora (2026-09-27) no coincide con la fecha del registro (2026-09-28)."}

        resp = self._entrada(hora="2026-09-27 08:00:00")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), esperado)

        self._entrada()
        resp = self._salida(hora="2026-09-27 18:00:00")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), esperado)

    def test_despues_de_un_409_el_patch_corrige_entrada_y_salida(self):
        self._entrada()
        self._salida()
        self.assertEqual(self._entrada(hora="2026-09-28 07:59:00").status_code, 409)
        self.assertEqual(self._salida(hora="2026-09-28 17:00:00").status_code, 409)
        asistencia = Asistencia.objects.get(empleado=self.empleado, fecha=DIA)

        resp = self._patch(asistencia, {
            "hora_entrada": _mx(DIA, 7, 59).isoformat(), "hora_salida": _mx(DIA, 17, 0).isoformat(),
        })

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["estado"], body["minutos_retardo"]), ("puntual", 0))
        self.assertEqual((body["horas_normales"], body["horas_extra"]), ("8.00", "1.02"))

    # -- Aislamiento por empresa -----------------------------------------------------

    def test_otra_empresa_sigue_viendo_404(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0))
        ajeno = self.b["usuario"]

        with self.subTest("detalle"):
            self.assertEqual(self._client(ajeno).get(f"{ASISTENCIAS_URL}{asistencia.pk}/").status_code, 404)
        with self.subTest("patch"):
            self.assertEqual(self._patch(asistencia, {"observaciones": "x"}, user=ajeno).status_code, 404)
        for url in (ENTRADA_URL, SALIDA_URL):
            with self.subTest(url=url):
                resp = self._client(ajeno).post(
                    url, {"empleado_id": self.empleado.pk, "fecha": DIA.isoformat(), "hora": "2026-09-28 08:00:00"},
                    format="json",
                )
                self.assertEqual(resp.status_code, 404, resp.content)
                self.assertEqual(resp.json(), {"detail": "Empleado no encontrado."})


MENSAJE_ENTRADA_TRAS_SALIDA = "La hora de entrada debe ser anterior a la hora de salida registrada."
MENSAJE_SALIDA_NO_POSTERIOR = "La hora de salida debe ser posterior a la de entrada."


class AsistenciaRevisionTests(AsistenciaBase):
    """Correcciones de la revisión de código del paquete de asistencias."""

    def _entrada(self, empleado=None, **data):
        cuerpo = {
            "empleado_id": (empleado or self.empleado).pk,
            "fecha": DIA.isoformat(),
            "hora": "2026-09-28 08:12:00",
        }
        cuerpo.update(data)
        return self._client().post(ENTRADA_URL, cuerpo, format="json")

    # -- 1. Registro existente con salida y sin entrada ------------------------------

    def test_entrada_en_o_despues_de_la_salida_guardada_responde_400(self):
        # Una fila así solo se crea por el ORM o datos previos: el serializer ya
        # rechaza una salida sin entrada.
        asistencia = self._asistencia(hora_salida=_mx(DIA, 13, 0))

        for hora in ("2026-09-28 13:00:00", "2026-09-28 14:00:00"):
            with self.subTest(hora=hora):
                resp = self._entrada(hora=hora)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), {"detail": MENSAJE_ENTRADA_TRAS_SALIDA})
        asistencia.refresh_from_db()
        self.assertIsNone(asistencia.hora_entrada)

        resp = self._entrada(hora="2026-09-28 08:00:00")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["horas_normales"], resp.json()["horas_extra"]), ("5.00", "0.00"))

    # -- 2. Invariantes del modelo (ruta del admin) -----------------------------------

    def test_clean_aplica_las_reglas_de_fecha_y_orden(self):
        base = {"empleado": self.empleado, "turno": self.turno, "fecha": DIA}
        casos = {
            "entrada de otro día": (
                {"hora_entrada": _mx(date(2026, 9, 27), 8, 0)},
                {"hora_entrada": ["La hora de entrada debe corresponder a la fecha de la asistencia."]},
            ),
            "salida de otro día": (
                {"hora_entrada": _mx(DIA, 8, 0), "hora_salida": _mx(date(2026, 10, 3), 17, 0)},
                {"hora_salida": ["La hora de salida debe corresponder a la fecha de la asistencia."]},
            ),
            "salida sin entrada": (
                {"hora_salida": _mx(DIA, 17, 0)},
                {"hora_salida": ["No se puede registrar la salida sin una hora de entrada."]},
            ),
            "turno de duración cero": (
                {"hora_entrada": _mx(DIA, 8, 0), "hora_salida": _mx(DIA, 8, 0)},
                {"hora_salida": [MENSAJE_SALIDA_NO_POSTERIOR]},
            ),
            "salida anterior": (
                {"hora_entrada": _mx(DIA, 8, 0), "hora_salida": _mx(DIA, 7, 0)},
                {"hora_salida": [MENSAJE_SALIDA_NO_POSTERIOR]},
            ),
        }
        for nombre, (horas, esperado) in casos.items():
            with self.subTest(nombre):
                with self.assertRaises(DjangoValidationError) as ctx:
                    Asistencia(**base, **horas).full_clean()
                self.assertEqual(ctx.exception.message_dict, esperado)

        with self.subTest("registro válido"):
            Asistencia(**base, hora_entrada=_mx(DIA, 8, 0), hora_salida=_mx(DIA, 17, 0)).full_clean()

    # -- 3. La checada libera la justificación ---------------------------------------

    def test_registrar_entrada_libera_una_justificacion_sin_entrada(self):
        for hora, esperado in (("2026-09-28 08:30:00", "retardo"), ("2026-09-28 08:00:00", "puntual")):
            with self.subTest(esperado=esperado):
                empleado = self._empleado(self.a, f"E-J-{esperado}")
                Empleado.objects.filter(pk=empleado.pk).update(turno=self.turno)
                justificada = self._asistencia(empleado=empleado, estado="justificada")
                self.assertEqual(justificada.estado, "justificada")

                resp = self._entrada(empleado=empleado, hora=hora)

                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual((resp.json()["id"], resp.json()["estado"]), (justificada.pk, esperado))

    def test_una_justificacion_por_patch_sigue_fija(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 30))

        resp = self._patch(asistencia, {"estado": "justificada", "hora_entrada": _mx(DIA, 8, 40).isoformat()})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["estado"], resp.json()["minutos_retardo"]), ("justificada", 35))

    # -- 4. Creación concurrente contra una fila sin entrada -------------------------

    def test_creacion_concurrente_contra_una_fila_sin_entrada_la_completa(self):
        # La fila rival no tiene entrada (una falta capturada a mano): el INSERT
        # choca, se relee y se le pone la entrada en vez de responder 409.
        rival = self._asistencia()

        with patch("hr.api.views.AsistenciaViewSet._asistencia_del_dia", return_value=None):
            resp = self._entrada()

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual((resp.json()["id"], resp.json()["estado"]), (rival.pk, "retardo"))
        self.assertEqual(Asistencia.objects.filter(empleado=self.empleado, fecha=DIA).count(), 1)

    # -- 5. Turno de duración cero en el serializer ----------------------------------

    def test_patch_con_salida_igual_a_la_entrada_responde_400(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0))

        resp = self._patch(asistencia, {"hora_salida": _mx(DIA, 8, 0).isoformat()})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"hora_salida": [MENSAJE_SALIDA_NO_POSTERIOR]})

    # -- 6. Llave del error al quitar la entrada -------------------------------------

    def test_quitar_solo_la_entrada_con_salida_guardada_reporta_en_hora_entrada(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0), hora_salida=_mx(DIA, 17, 0))

        resp = self._patch(asistencia, {"hora_entrada": None})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json(),
            {"hora_entrada": ["No se puede quitar la hora de entrada mientras haya una hora de salida."]},
        )

    def test_quitar_entrada_y_salida_juntas_deja_falta(self):
        asistencia = self._asistencia(hora_entrada=_mx(DIA, 8, 0), hora_salida=_mx(DIA, 17, 0))

        resp = self._patch(asistencia, {"hora_entrada": None, "hora_salida": None})

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["estado"], "falta")
        self.assertIsNone(body["horas_normales"])
        self.assertIsNone(body["horas_extra"])


CUERPO_UNICIDAD = {"non_field_errors": ["Los campos empleado, fecha deben formar un conjunto único."]}


class AsistenciaCarreraDeUnicidadTests(AsistenciaBase):
    """La carrera entre el alta/edición normal y otra escritura del mismo día.

    El validador de unicidad de DRF pasa, pero antes del INSERT/UPDATE otra
    petición (p. ej. ``registrar_entrada``) crea la fila de ``(empleado, fecha)``.
    La constraint lo detiene y el cliente recibe el mismo 400 del validador, no
    un 500.
    """

    def _perder_la_carrera(self, fecha):
        validate_original = AsistenciaSerializer.validate

        def validate_y_pierde_la_carrera(serializer, data):
            data = validate_original(serializer, data)
            self._asistencia(fecha=fecha, hora_entrada=_mx(fecha, 8, 0))
            return data

        return patch.object(AsistenciaSerializer, "validate", validate_y_pierde_la_carrera)

    def test_el_cuerpo_de_la_ruta_normal_es_el_esperado(self):
        # Ancla: la carrera debe devolver exactamente este cuerpo.
        self._asistencia()

        resp = self._post(self._payload())

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), CUERPO_UNICIDAD)

    def test_alta_que_pierde_la_carrera_responde_400_y_no_500(self):
        with self._perder_la_carrera(DIA):
            resp = self._post(self._payload())

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), CUERPO_UNICIDAD)
        self.assertEqual(Asistencia.objects.filter(empleado=self.empleado, fecha=DIA).count(), 1)

    def test_edicion_que_pierde_la_carrera_responde_400_y_no_500(self):
        otro_dia = date(2026, 9, 27)
        asistencia = self._asistencia(fecha=otro_dia)

        with self._perder_la_carrera(DIA):
            resp = self._patch(asistencia, {"fecha": DIA.isoformat()})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), CUERPO_UNICIDAD)
        asistencia.refresh_from_db()
        self.assertEqual(asistencia.fecha, otro_dia)

    def test_otro_integrity_error_se_propaga(self):
        # Sin fila rival, un IntegrityError no es la colisión de unicidad: no se
        # disfraza de 400.
        with patch.object(Asistencia, "save", side_effect=IntegrityError("otra constraint")):
            with self.assertRaises(IntegrityError):
                self._post(self._payload())
