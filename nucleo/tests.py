"""Tests del buscador global federado (``GET /api/v1/search/``).

Lo que se cubre: aislamiento multi-tenant por entidad (que es lo único que separa
este endpoint de una fuga cross-tenant), la longitud mínima de ``q``, el tope de
``limit``, y que cada entidad conserve EXACTAMENTE el alcance de su propio ViewSet
—incluidos los scopes extra por ``vendedor``/``vendedores`` que no todas tienen—.

Ejecutar SIEMPRE con una BD desechable; el ``.env`` del repo apunta a Supabase de
producción. Ejemplo con un settings de override a SQLite en memoria:

    python manage.py test nucleo --settings=sqlite_settings
"""

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from catalogo.models import Color, Producto, ProductoVariante, Talla
from finanzas.models import Factura
from nucleo.api.search import REGISTRO
from nucleo.models import Empresa, Moneda, Sucursal
from nucleo.permisos import permisos_efectivos
from produccion.models import OrdenesBordado, OrdenesReflejante
from seguridad.models import Permiso, Rol, RolPermiso, UsuarioPermiso, UsuarioRol
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Cotizacion, Pedido

SEARCH_URL = "/api/v1/search/"
COTIZACIONES_URL = "/api/v1/ventas/cotizaciones/"

#: Grupos que ve quien pasa el cortocircuito de permisos (superuser/admin), en el
#: orden del ``REGISTRO``.
TODOS_LOS_TIPOS = [
    "pedido", "orden_bordado", "orden_reflejante",
    "cliente", "cotizacion", "producto", "factura",
]


class BusquedaGlobalBaseTestCase(TestCase):
    """Dos empresas cuyos datos comparten el término buscado ("acme").

    Es deliberado: si el aislamiento fallara, buscar "acme" traería filas de ambas
    y los tests lo verían.
    """

    @classmethod
    def _tenant(cls, codigo, sufijo_folio, etiqueta):
        empresa = Empresa.objects.create(codigo=codigo, razon_social=f"{codigo} SA")
        sucursal = Sucursal.objects.create(
            empresa=empresa, codigo=codigo[:3].upper(), nombre=codigo
        )
        cliente = Cliente.objects.create(
            empresa=empresa,
            nombre=f"Comercial Acme {etiqueta}",
            razon_social=f"COMERCIAL ACME {etiqueta} SA DE CV",
            correo=f"ventas@acme-{etiqueta.lower()}.test",
        )
        admin = Usuario.objects.create(
            username=f"admin-{codigo}",
            email=f"admin@{codigo}.test",
            empresa=empresa,
            sucursal_default=sucursal,
            is_admin_empresa=True,
        )
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=cliente,
            moneda=cls.moneda,
            folio=f"P-{sufijo_folio}",
            cliente_nombre=cliente.nombre,
            cliente_razon_social=cliente.razon_social,
            persona_pagos="Pagos",
            correo_facturas=f"pagos@{codigo}.test",
            telefono_pagos="8100000000",
            forma_pago="03",
            metodo_pago="PUE",
            uso_cfdi="G03",
        )
        cotizacion = Cotizacion.objects.create(
            empresa=empresa, sucursal=sucursal, cliente=cliente, vendedor=admin
        )
        # Formato real de producción (``<año>-OB-<consecutivo>``). ``OB-000`` está
        # en el folio de ambas empresas por diseño: una fuga de aislamiento se
        # vería al buscarlo.
        orden_bordado = OrdenesBordado.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            pedido=pedido,
            folio_bordado=f"2026-OB-{sufijo_folio}",
        )
        return {
            "empresa": empresa,
            "sucursal": sucursal,
            "cliente": cliente,
            "admin": admin,
            "pedido": pedido,
            "cotizacion": cotizacion,
            "orden_bordado": orden_bordado,
        }

    @classmethod
    def setUpTestData(cls):
        cls.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso")
        cls.a = cls._tenant("acme-search", "00027", "Norte")
        cls.b = cls._tenant("globex-search", "00028", "Sur")

        cls.sin_empresa = Usuario.objects.create(
            username="huerfano-search", email="huerfano@nowhere-search.test"
        )
        cls.superuser = Usuario.objects.create(
            username="root-search", email="root@nowhere-search.test", is_superuser=True
        )
        # Usuario normal de la empresa A: NO es admin, NO está en
        # ``cliente.vendedores`` y NO es vendedor de la cotización. Debe ver el
        # pedido (Pedido no tiene scope por vendedor) pero ni el cliente ni la
        # cotización.
        #
        # Lleva el rol de ventas real para que las tres entidades le sean VISIBLES:
        # sin permisos, el buscador omitiría los grupos y no se podría comprobar el
        # alcance de fila, que es lo que estos tests miden.
        cls.vendedor_a = Usuario.objects.create(
            username="vendedor-a-search",
            email="vendedor@acme-search.test",
            empresa=cls.a["empresa"],
            sucursal_default=cls.a["sucursal"],
        )
        cls.rol_ventas = cls._rol_con(
            cls.a["empresa"],
            "ventas-test",
            ["R-CRM-PEDIDOS", "R-CRM-CLIENTES", "R-CRM-COTIZACIONES"],
        )
        UsuarioRol.objects.create(
            usuario=cls.vendedor_a, rol=cls.rol_ventas, empresa=cls.a["empresa"]
        )

    @classmethod
    def _rol_con(cls, empresa, codigo, claves):
        """Crea un rol activo con esos permisos del catálogo (creándolos si faltan).

        El catálogo real vive sólo en la BD (no hay fixture), así que las pruebas
        siembran las claves que necesitan. Se usan las cadenas EXACTAS del catálogo
        de producción.
        """
        rol = Rol.objects.create(
            empresa=empresa, codigo=codigo, nombre=codigo, estatus=Rol.Estatus.ACTIVO
        )
        for clave in claves:
            permiso, _ = Permiso.objects.get_or_create(
                clave=clave, defaults={"nombre": clave}
            )
            RolPermiso.objects.create(rol=rol, permiso=permiso)
        return rol

    def _buscar(self, user, q, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        query = f"?q={q}" + "".join(f"&{k}={v}" for k, v in params.items())
        resp = client.get(f"{SEARCH_URL}{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _ids(self, payload, tipo):
        for grupo in payload["grupos"]:
            if grupo["tipo"] == tipo:
                return [fila["id"] for fila in grupo["resultados"]]
        self.fail(f"no vino el grupo {tipo}")


class PermisosEfectivosEquivalenciaTests(BusquedaGlobalBaseTestCase):
    """``permisos_efectivos()`` decide EXACTAMENTE lo mismo que ``tiene_permiso()``.

    Son dos implementaciones deliberadamente independientes de la misma precedencia
    (ver ``nucleo/permisos.py``), así que lo único que impide que se separen es esta
    comparación caso por caso. Si alguna vez divergen, falla aquí.
    """

    #: Todas las claves que el buscador consulta, más una que no existe en catálogo.
    CLAVES = tuple(
        clave for entidad in REGISTRO for clave in entidad.permisos_visibilidad
    ) + ("R-NO-EXISTE",)

    def _assert_equivalente(self, user, nota=""):
        # El resolutor se construye UNA vez y se consulta muchas: es justo lo que
        # el módulo predica, y evita rehacer las 2 consultas por cada clave.
        permisos = permisos_efectivos(user)
        for clave in self.CLAVES:
            with self.subTest(usuario=user.username, clave=clave, caso=nota):
                self.assertEqual(
                    clave in permisos,
                    user.tiene_permiso(clave),
                    f"divergencia en {clave} para {user.username} ({nota})",
                )

    def _usuario(self, sufijo, claves=(), estatus=None):
        user = Usuario.objects.create(
            username=f"eq-{sufijo}",
            email=f"eq-{sufijo}@acme-search.test",
            empresa=self.a["empresa"],
            sucursal_default=self.a["sucursal"],
        )
        if claves:
            rol = self._rol_con(self.a["empresa"], f"roleq-{sufijo}", claves)
            if estatus:
                rol.estatus = estatus
                rol.save(update_fields=["estatus"])
            UsuarioRol.objects.create(usuario=user, rol=rol, empresa=self.a["empresa"])
        return user

    def _override(self, user, clave, tipo):
        permiso, _ = Permiso.objects.get_or_create(
            clave=clave, defaults={"nombre": clave}
        )
        UsuarioPermiso.objects.create(usuario=user, permiso=permiso, tipo=tipo)

    # --- los seis casos de la precedencia -------------------------------------

    def test_equivalente_para_superuser(self):
        self._assert_equivalente(self.superuser, "superuser")

    def test_equivalente_para_admin_empresa(self):
        self._assert_equivalente(self.a["admin"], "admin_empresa")

    def test_superuser_con_deny_explicito_sigue_viendolo_todo(self):
        """El cortocircuito va ANTES que la resta de denegados, en ambas rutas.

        Es el cruce que distingue una implementación correcta de una que calcule
        el conjunto primero y aplique ``todo`` después.
        """
        self._override(self.superuser, "R-CRM-CLIENTES", UsuarioPermiso.TIPO_DENY)
        self.assertTrue(self.superuser.tiene_permiso("R-CRM-CLIENTES"))
        self.assertIn("R-CRM-CLIENTES", permisos_efectivos(self.superuser))
        self._assert_equivalente(self.superuser, "superuser con deny")

    def test_admin_empresa_con_deny_explicito_sigue_viendolo_todo(self):
        admin = self.a["admin"]
        self._override(admin, "R-CRM-CLIENTES", UsuarioPermiso.TIPO_DENY)
        self.assertTrue(admin.tiene_permiso("R-CRM-CLIENTES"))
        self.assertIn("R-CRM-CLIENTES", permisos_efectivos(admin))
        self._assert_equivalente(admin, "admin con deny")

    def test_admin_con_deny_sigue_viendo_el_grupo_en_la_respuesta(self):
        """Y el efecto llega hasta el endpoint, no sólo al resolutor."""
        self._override(self.a["admin"], "R-CRM-CLIENTES", UsuarioPermiso.TIPO_DENY)
        tipos = [g["tipo"] for g in self._buscar(self.a["admin"], "acme")["grupos"]]
        self.assertIn("cliente", tipos)

    def test_equivalente_sin_roles_ni_overrides(self):
        self._assert_equivalente(self._usuario("pelado"), "fallo simple")

    def test_equivalente_con_grants_por_rol(self):
        user = self._usuario("conrol", ["R-CRM-PEDIDOS", "R-CRM-CLIENTES"])
        self._assert_equivalente(user, "grants por rol")

    def test_equivalente_con_rol_inactivo(self):
        user = self._usuario("inactivo", ["R-CRM-CLIENTES"], estatus=Rol.Estatus.INACTIVO)
        self.assertFalse(user.tiene_permiso("R-CRM-CLIENTES"))
        self._assert_equivalente(user, "rol inactivo")

    def test_equivalente_con_grant_sin_rol(self):
        user = self._usuario("grant")
        self._override(user, "R-MESACONTROL-COTI", UsuarioPermiso.TIPO_GRANT)
        self.assertTrue(user.tiene_permiso("R-MESACONTROL-COTI"))
        self._assert_equivalente(user, "grant sin rol")

    def test_equivalente_con_deny_que_gana_al_rol(self):
        """El caso que la BD real no tiene hoy: DENY explícito sobre un rol que concede."""
        user = self._usuario("deny", ["R-CRM-CLIENTES", "R-CRM-PEDIDOS"])
        self._override(user, "R-CRM-CLIENTES", UsuarioPermiso.TIPO_DENY)

        self.assertFalse(user.tiene_permiso("R-CRM-CLIENTES"))
        self.assertNotIn("R-CRM-CLIENTES", permisos_efectivos(user))
        # El otro permiso del mismo rol NO se ve afectado.
        self.assertTrue(user.tiene_permiso("R-CRM-PEDIDOS"))
        self.assertIn("R-CRM-PEDIDOS", permisos_efectivos(user))
        self._assert_equivalente(user, "deny gana al rol")

    def test_equivalente_con_deny_y_grant_sobre_la_misma_clave(self):
        """Con ambos overrides, DENY gana en las dos implementaciones."""
        user = self._usuario("ambos")
        self._override(user, "R-CRM-CLIENTES", UsuarioPermiso.TIPO_GRANT)
        self._override(user, "R-CRM-CLIENTES", UsuarioPermiso.TIPO_DENY)
        self.assertFalse(user.tiene_permiso("R-CRM-CLIENTES"))
        self._assert_equivalente(user, "deny + grant")

    # --- equivalencia de la DECISIÓN de visibilidad, rol por rol --------------

    def test_misma_decision_por_entidad_para_los_roles_reales(self):
        """Réplica de los 6 roles del catálogo real, comparando ruta vieja y nueva."""
        roles_reales = {
            "ventas": ["R-CRM", "R-CRM-CLIENTES", "R-CRM-COTIZACIONES", "R-CRM-PEDIDOS"],
            "mesacontrol": [
                "R-MESACONTROL", "R-MESACONTROL-CLIENTES",
                "R-MESACONTROL-COTI", "R-MESACONTROL-PEDIDOS",
            ],
            "wms": ["R-WMS", "R-WMS-PEDIDOS", "R-WMS-PICKING"],
            "compras": ["R-COMPRAS", "R-COMPRAS-OC", "R-COMPRAS-PEDIDOS"],
            "produccion": ["R-PRODUCCION", "R-PRODUCCION-OB", "R-PRODUCCION-OR", "R-PRODUCCION-CM"],
            "contabilidad": [
                "R-CONTABILIDAD", "R-CONTABILIDAD-CLIENTES",
                "R-CONTABILIDAD-CXC", "R-CONTABILIDAD-FACTURACION",
            ],
        }
        for nombre, claves in roles_reales.items():
            user = self._usuario(f"rol-{nombre}", claves)
            permisos = permisos_efectivos(user)
            for entidad in REGISTRO:
                viejo = any(
                    user.tiene_permiso(c) for c in entidad.permisos_visibilidad
                )
                nuevo = entidad.visible_para(permisos)
                with self.subTest(rol=nombre, entidad=entidad.tipo):
                    self.assertEqual(nuevo, viejo)

    # --- coste ----------------------------------------------------------------

    def test_superuser_y_admin_no_gastan_consultas_en_permisos(self):
        for user, nota in ((self.superuser, "superuser"), (self.a["admin"], "admin")):
            with self.subTest(caso=nota), self.assertNumQueries(0):
                permisos_efectivos(user)

    def test_resolver_gasta_dos_consultas_pase_lo_que_pase(self):
        """Coste constante: no depende de cuántas claves se consulten después."""
        user = self._usuario("coste", ["R-CRM-PEDIDOS"])
        with self.assertNumQueries(2):
            permisos = permisos_efectivos(user)
        # Consultar las 16 claves no dispara ni una consulta más.
        with self.assertNumQueries(0):
            for entidad in REGISTRO:
                entidad.visible_para(permisos)

    def test_la_peticion_completa_no_paga_permisos_por_entidad(self):
        """Ancla el coste de la PETICIÓN, que es la métrica que motivó el cambio.

        Sin esto, mover ``permisos_efectivos()`` dentro del bucle del ``REGISTRO``
        volvería a cobrar 2 consultas por entidad y las pruebas seguirían verdes:
        la otra medición sólo cubre la función en aislamiento, nunca la vista.

        El usuario que ve UN grupo y el que ve TRES pagan los mismos permisos; lo
        único que los separa son las consultas de búsqueda, una por entidad.
        """
        un_grupo = self._usuario("coste-1", ["R-COMPRAS-PEDIDOS"])
        tres_grupos = self._usuario(
            "coste-3", ["R-COMPRAS-PEDIDOS", "R-CRM-CLIENTES", "R-CRM-COTIZACIONES"]
        )

        def consultas(user):
            client = APIClient()
            client.force_authenticate(user=user)
            with CaptureQueriesContext(connection) as capturadas:
                resp = client.get(f"{SEARCH_URL}?q=acme")
            self.assertEqual(resp.status_code, 200)
            return len(capturadas)

        self.assertEqual([g["tipo"] for g in self._buscar(un_grupo, "acme")["grupos"]], ["pedido"])
        coste_1 = consultas(un_grupo)
        coste_3 = consultas(tres_grupos)

        # Dos grupos más = dos búsquedas más. Si los permisos se resolvieran por
        # entidad, la diferencia sería de 6 (2 búsquedas + 4 de permisos).
        self.assertEqual(coste_3 - coste_1, 2, f"{coste_1} -> {coste_3}")
        # Y el coste absoluto se mantiene pequeño: permisos + una consulta por grupo.
        self.assertLessEqual(coste_1, 4, f"la petición de 1 grupo costó {coste_1}")

    def test_usuario_anonimo_no_revienta_y_no_ve_nada(self):
        permisos = permisos_efectivos(AnonymousUser())
        self.assertNotIn("R-CRM-PEDIDOS", permisos)
        self.assertFalse(permisos.alguno(self.CLAVES))


class VisibilidadPorPermisosTests(BusquedaGlobalBaseTestCase):
    """Qué TIPOS de entidad ve cada usuario (distinto del alcance de fila).

    La entidad sin permiso se omite entera de ``grupos``; no sale como grupo vacío.
    """

    def _tipos(self, user, q="acme"):
        return [g["tipo"] for g in self._buscar(user, q)["grupos"]]

    def _usuario_con(self, sufijo, claves):
        user = Usuario.objects.create(
            username=f"perm-{sufijo}",
            email=f"perm-{sufijo}@acme-search.test",
            empresa=self.a["empresa"],
            sucursal_default=self.a["sucursal"],
        )
        rol = self._rol_con(self.a["empresa"], f"rol-{sufijo}", claves)
        UsuarioRol.objects.create(usuario=user, rol=rol, empresa=self.a["empresa"])
        return user

    # --- sin permisos no se ve ninguna sección --------------------------------

    def test_usuario_sin_permisos_no_recibe_ningun_grupo(self):
        huerfano = Usuario.objects.create(
            username="sin-permisos", email="sinperm@acme-search.test",
            empresa=self.a["empresa"], sucursal_default=self.a["sucursal"],
        )
        self.assertEqual(self._tipos(huerfano), [])

    def test_grupo_sin_permiso_se_omite_no_llega_vacio(self):
        """Lo importante: la clave NO está en la respuesta, no está en cero."""
        solo_clientes = self._usuario_con("cli", ["R-CRM-CLIENTES"])
        payload = self._buscar(solo_clientes, "acme")
        tipos = [g["tipo"] for g in payload["grupos"]]
        self.assertEqual(tipos, ["cliente"])
        self.assertNotIn("pedido", tipos)
        self.assertNotIn("cotizacion", tipos)

    # --- basta UNO de los códigos de la entidad -------------------------------

    def test_un_solo_codigo_de_pedido_basta(self):
        for clave in (
            "R-CRM-PEDIDOS",
            "R-WMS-PEDIDOS",
            "R-COMPRAS-PEDIDOS",
            "R-MESACONTROL-PEDIDOS",
            "R-PRODUCCION-OB",
            "R-PRODUCCION-OR",
            "R-PRODUCCION-CM",
            "R-COMPRAS-OC",
            "R-WMS-PICKING",
        ):
            with self.subTest(clave=clave):
                user = self._usuario_con(clave.lower(), [clave])
                self.assertIn("pedido", self._tipos(user))

    def test_un_solo_codigo_de_cliente_basta(self):
        for clave in ("R-CRM-CLIENTES", "R-MESACONTROL-CLIENTES", "R-CONTABILIDAD-CLIENTES"):
            with self.subTest(clave=clave):
                user = self._usuario_con(clave.lower(), [clave])
                self.assertIn("cliente", self._tipos(user))

    def test_un_solo_codigo_de_cotizacion_basta(self):
        for clave in ("R-CRM-COTIZACIONES", "R-CRM", "R-MESACONTROL-COTI", "R-MESACONTROL"):
            with self.subTest(clave=clave):
                user = self._usuario_con(clave.lower(), [clave])
                self.assertIn("cotizacion", self._tipos(user))

    def test_codigo_de_modulo_concede_cotizacion_sin_el_de_seccion(self):
        """``R-CRM`` y ``R-CRM-COTIZACIONES`` son cadenas planas e independientes."""
        user = self._usuario_con("solo-modulo", ["R-CRM"])
        self.assertIn("cotizacion", self._tipos(user))

    def test_codigo_de_otra_entidad_no_concede_esta(self):
        """Tener pedidos no da clientes: las reglas no se contagian."""
        user = self._usuario_con("solo-ped", ["R-WMS-PEDIDOS"])
        tipos = self._tipos(user)
        self.assertEqual(tipos, ["pedido"])

    # --- superuser / admin salen gratis por el cortocircuito ------------------

    def test_superuser_ve_todas_sin_permiso_alguno(self):
        self.assertEqual(self._tipos(self.superuser), TODOS_LOS_TIPOS)

    def test_admin_empresa_ve_todas_sin_permiso_alguno(self):
        self.assertEqual(self._tipos(self.a["admin"]), TODOS_LOS_TIPOS)

    # --- overrides: DENY gana sobre el rol ------------------------------------

    def test_override_deny_oculta_la_entidad_pese_al_rol(self):
        user = self._usuario_con("denegado", ["R-CRM-CLIENTES"])
        self.assertIn("cliente", self._tipos(user))
        UsuarioPermiso.objects.create(
            usuario=user,
            permiso=Permiso.objects.get(clave="R-CRM-CLIENTES"),
            tipo=UsuarioPermiso.TIPO_DENY,
        )
        self.assertNotIn("cliente", self._tipos(user))

    def test_override_grant_concede_la_entidad_sin_rol(self):
        user = Usuario.objects.create(
            username="con-grant", email="grant@acme-search.test",
            empresa=self.a["empresa"], sucursal_default=self.a["sucursal"],
        )
        permiso, _ = Permiso.objects.get_or_create(
            clave="R-MESACONTROL-COTI", defaults={"nombre": "R-MESACONTROL-COTI"}
        )
        UsuarioPermiso.objects.create(
            usuario=user, permiso=permiso, tipo=UsuarioPermiso.TIPO_GRANT
        )
        self.assertEqual(self._tipos(user), ["cotizacion"])

    # --- la omisión se mantiene con q corta -----------------------------------

    def test_con_q_corta_tambien_se_omiten_los_grupos_sin_permiso(self):
        user = self._usuario_con("qcorta", ["R-CRM-CLIENTES"])
        self.assertEqual([g["tipo"] for g in self._buscar(user, "a")["grupos"]], ["cliente"])

    def test_rol_inactivo_no_concede_visibilidad(self):
        user = self._usuario_con("inactivo", ["R-CRM-CLIENTES"])
        self.assertIn("cliente", self._tipos(user))
        rol = Rol.objects.get(codigo="rol-inactivo")
        rol.estatus = Rol.Estatus.INACTIVO
        rol.save(update_fields=["estatus"])
        self.assertEqual(self._tipos(user), [])


class AislamientoMultiTenantTests(BusquedaGlobalBaseTestCase):
    """Lo crítico: la empresa A nunca ve filas de la empresa B."""

    def test_pedido_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_cliente_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_cotizacion_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "cotizacion"), [self.a["cotizacion"].pk])

    def test_folio_de_otra_empresa_no_aparece_por_prefijo(self):
        """``P-000`` es prefijo de los folios de AMBAS empresas."""
        payload = self._buscar(self.a["admin"], "P-000")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_usuario_sin_empresa_no_ve_nada(self):
        """No tiene permisos ni empresa: le corta primero el filtro de visibilidad.

        Se afirma el conjunto vacío explícitamente en vez de recorrer ``grupos``,
        que sin esto pasaría de forma vacía (cero iteraciones, cero asserts).
        """
        payload = self._buscar(self.sin_empresa, "acme")
        self.assertEqual(payload["grupos"], [])

    def test_usuario_con_permisos_pero_sin_empresa_no_ve_filas(self):
        """La capa de tenant sigue viva por debajo de la de permisos.

        Con los permisos concedidos, los tres grupos LLEGAN, pero vacíos: el
        alcance por empresa es el que deja fuera las filas.
        """
        huerfano = Usuario.objects.create(
            username="huerfano-conperm", email="huerfano-cp@nowhere-search.test"
        )
        rol = self._rol_con(
            self.a["empresa"],
            "rol-huerfano",
            ["R-CRM-PEDIDOS", "R-CRM-CLIENTES", "R-CRM-COTIZACIONES"],
        )
        UsuarioRol.objects.create(usuario=huerfano, rol=rol, empresa=self.a["empresa"])

        payload = self._buscar(huerfano, "acme")
        self.assertEqual(
            [g["tipo"] for g in payload["grupos"]], ["pedido", "cliente", "cotizacion"]
        )
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])

    def test_usuario_no_autenticado_es_rechazado(self):
        resp = APIClient().get(f"{SEARCH_URL}?q=acme")
        self.assertIn(resp.status_code, (401, 403))


class AlcancePorEntidadTests(BusquedaGlobalBaseTestCase):
    """Cada entidad conserva el alcance de SU ViewSet, no uno inventado aquí."""

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, "acme")
        self.assertEqual(
            sorted(self._ids(payload, "pedido")),
            sorted([self.a["pedido"].pk, self.b["pedido"].pk]),
        )
        self.assertEqual(
            sorted(self._ids(payload, "cliente")),
            sorted([self.a["cliente"].pk, self.b["cliente"].pk]),
        )
        self.assertEqual(
            sorted(self._ids(payload, "cotizacion")),
            sorted([self.a["cotizacion"].pk, self.b["cotizacion"].pk]),
        )

    def test_vendedor_no_admin_ve_el_pedido_pero_no_el_cliente_ni_la_cotizacion(self):
        """``Pedido`` no tiene scope por vendedor; ``Cliente`` y ``Cotizacion`` sí."""
        payload = self._buscar(self.vendedor_a, "acme")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])
        self.assertEqual(self._ids(payload, "cliente"), [])
        self.assertEqual(self._ids(payload, "cotizacion"), [])

    def test_vendedor_asignado_si_ve_su_cliente_y_su_cotizacion(self):
        self.a["cliente"].vendedores.add(self.vendedor_a)
        self.a["cotizacion"].vendedor = self.vendedor_a
        self.a["cotizacion"].save(update_fields=["vendedor"])

        payload = self._buscar(self.vendedor_a, "acme")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])
        self.assertEqual(self._ids(payload, "cotizacion"), [self.a["cotizacion"].pk])

    def test_pedido_con_soft_delete_no_aparece(self):
        self.a["pedido"].soft_delete()
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(self._ids(payload, "pedido"), [])


class CoincidenciaYFormaTests(BusquedaGlobalBaseTestCase):
    def test_q_corta_devuelve_grupos_vacios_no_error(self):
        payload = self._buscar(self.a["admin"], "a")
        self.assertEqual([g["tipo"] for g in payload["grupos"]], TODOS_LOS_TIPOS)
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])
            self.assertFalse(grupo["hay_mas"])

    def test_q_ausente_devuelve_grupos_vacios_no_error(self):
        client = APIClient()
        client.force_authenticate(user=self.a["admin"])
        resp = client.get(SEARCH_URL)
        self.assertEqual(resp.status_code, 200)
        for grupo in resp.json()["grupos"]:
            self.assertEqual(grupo["resultados"], [])

    def test_codigo_hace_prefijo_no_subcadena(self):
        """``folio`` es CÓDIGO: ``00027`` NO debe encontrar ``P-00027``."""
        payload = self._buscar(self.a["admin"], "00027")
        self.assertEqual(self._ids(payload, "pedido"), [])
        # El mismo folio, buscado por su prefijo real, sí aparece.
        payload = self._buscar(self.a["admin"], "p-00027")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])

    def test_q_de_2_caracteres_solo_consulta_campos_codigo(self):
        """Con 2 caracteres pg_trgm no puede usar el índice: sólo va por CÓDIGO.

        Y lo importante: los grupos sin campos CÓDIGO deben quedar VACÍOS, no
        devolver la tabla entera. Un ``Q()`` vacío no filtra nada.
        """
        payload = self._buscar(self.a["admin"], "P-")
        self.assertEqual(self._ids(payload, "pedido"), [self.a["pedido"].pk])
        self.assertEqual(self._ids(payload, "cliente"), [])
        self.assertEqual(self._ids(payload, "cotizacion"), [])

    def test_q_de_2_caracteres_que_no_es_folio_no_devuelve_nada(self):
        """"Ac" está en el nombre del cliente, pero 2 caracteres no van a NOMBRE."""
        payload = self._buscar(self.a["admin"], "Ac")
        for grupo in payload["grupos"]:
            self.assertEqual(grupo["resultados"], [], grupo["tipo"])

    def test_a_partir_de_3_caracteres_si_entra_el_campo_nombre(self):
        payload = self._buscar(self.a["admin"], "Acm")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_nombre_hace_subcadena(self):
        """Los campos NOMBRE sí son subcadena: "Acme" está en medio de la razón social."""
        payload = self._buscar(self.a["admin"], "ACME NORTE")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_cliente_se_encuentra_por_correo(self):
        payload = self._buscar(self.a["admin"], "acme-norte.test")
        self.assertEqual(self._ids(payload, "cliente"), [self.a["cliente"].pk])

    def test_fila_lleva_lo_necesario_para_navegar(self):
        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "pedido")["resultados"][0]
        self.assertEqual(
            set(fila), {"tipo", "id", "codigo", "titulo", "subtitulo", "estatus"}
        )
        self.assertEqual(fila["codigo"], "P-00027")
        self.assertEqual(fila["subtitulo"], self.a["cliente"].razon_social)
        # Nada de URLs en el payload: el frontend arma la ruta con el id/folio.
        self.assertNotIn("url", fila)

    def test_pedido_sin_folio_no_deja_el_titulo_vacio(self):
        """``Pedido.folio`` es NULL-able (un BORRADOR aún no pasó por el folio)."""
        self.a["pedido"].folio = None
        self.a["pedido"].save(update_fields=["folio"])

        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "pedido")["resultados"][0]
        self.assertIsNone(fila["codigo"])
        self.assertEqual(fila["titulo"], self.a["cliente"].razon_social)

    def test_respuesta_expone_el_umbral_real_de_los_campos_nombre(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(payload["longitud_minima"], 2)
        self.assertEqual(payload["longitud_minima_nombre"], 3)

    def test_cotizacion_no_inventa_codigo(self):
        payload = self._buscar(self.a["admin"], "acme")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "cotizacion")["resultados"][0]
        self.assertIsNone(fila["codigo"])
        self.assertEqual(fila["id"], self.a["cotizacion"].pk)


class LimitTests(BusquedaGlobalBaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for i in range(8):
            Cliente.objects.create(
                empresa=cls.a["empresa"],
                nombre=f"Comercial Acme Extra {i}",
                razon_social=f"COMERCIAL ACME EXTRA {i}",
            )

    def test_limit_por_defecto_es_5(self):
        payload = self._buscar(self.a["admin"], "acme")
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(len(self._ids(payload, "cliente")), 5)

    def test_limit_recorta_por_grupo_y_marca_hay_mas(self):
        payload = self._buscar(self.a["admin"], "acme", limit=2)
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "cliente")
        self.assertEqual(len(grupo["resultados"]), 2)
        self.assertTrue(grupo["hay_mas"])

    def test_hay_mas_es_falso_cuando_cabe_todo(self):
        payload = self._buscar(self.a["admin"], "acme", limit=25)
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "pedido")
        self.assertFalse(grupo["hay_mas"])

    def test_limit_tiene_tope_duro(self):
        payload = self._buscar(self.a["admin"], "acme", limit=9999)
        self.assertEqual(payload["limit"], 25)

    def test_limit_invalido_cae_al_por_defecto(self):
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit="abc")["limit"], 5)

    def test_limit_cero_o_negativo_se_sube_a_1(self):
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit=0)["limit"], 1)
        self.assertEqual(self._buscar(self.a["admin"], "acme", limit=-3)["limit"], 1)


class OrdenBordadoBusquedaTests(BusquedaGlobalBaseTestCase):
    """Órdenes de bordado: visibilidad, alcance de fila y forma de la fila.

    El alcance es el de ``OrdenBordadoViewSet`` (``produccion.scope``): empresa,
    ``activo=True`` y, para quien no es admin, sólo sus ``sucursales_permitidas()``.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Segunda sucursal de la empresa A, con su propia OB. Los usuarios de
        # producción de abajo tienen como única sucursal la principal de A.
        cls.sucursal_a2 = Sucursal.objects.create(
            empresa=cls.a["empresa"], codigo="AC2", nombre="acme-search 2"
        )
        cls.ob_otra_sucursal = OrdenesBordado.objects.create(
            empresa=cls.a["empresa"],
            sucursal=cls.sucursal_a2,
            pedido=cls.a["pedido"],
            folio_bordado="2026-OB-00029",
        )

    def _usuario_con(self, sufijo, claves=()):
        user = Usuario.objects.create(
            username=f"ob-{sufijo}",
            email=f"ob-{sufijo}@acme-search.test",
            empresa=self.a["empresa"],
            sucursal_default=self.a["sucursal"],
        )
        if claves:
            rol = self._rol_con(self.a["empresa"], f"rol-ob-{sufijo}", claves)
            UsuarioRol.objects.create(usuario=user, rol=rol, empresa=self.a["empresa"])
        return user

    def _tipos(self, user, q="OB-"):
        return [g["tipo"] for g in self._buscar(user, q)["grupos"]]

    # --- visibilidad ----------------------------------------------------------

    def test_codigo_de_seccion_basta(self):
        self.assertIn("orden_bordado", self._tipos(self._usuario_con("sec", ["R-PRODUCCION-OB"])))

    def test_codigo_de_modulo_solo_no_basta(self):
        """``R-PRODUCCION`` no abre la sección de OB en el frontend."""
        self.assertNotIn("orden_bordado", self._tipos(self._usuario_con("mod", ["R-PRODUCCION"])))

    def test_sin_el_codigo_de_seccion_se_omite(self):
        user = self._usuario_con("otros", ["R-PRODUCCION-OR", "R-PRODUCCION-CM", "R-CRM-PEDIDOS"])
        self.assertNotIn("orden_bordado", self._tipos(user))

    def test_deny_oculta_la_entidad_si_nada_mas_la_concede(self):
        user = self._usuario_con("deny", ["R-PRODUCCION-OB"])
        self.assertIn("orden_bordado", self._tipos(user))
        UsuarioPermiso.objects.create(
            usuario=user,
            permiso=Permiso.objects.get(clave="R-PRODUCCION-OB"),
            tipo=UsuarioPermiso.TIPO_DENY,
        )
        self.assertNotIn("orden_bordado", self._tipos(user))

    # --- alcance de fila ------------------------------------------------------

    def test_ob_de_otra_empresa_no_aparece(self):
        """``OB-000`` está en el folio de las OBs de AMBAS empresas."""
        payload = self._buscar(self.a["admin"], "OB-000")
        self.assertEqual(
            sorted(self._ids(payload, "orden_bordado")),
            sorted([self.a["orden_bordado"].pk, self.ob_otra_sucursal.pk]),
        )
        self.assertNotIn(self.b["orden_bordado"].pk, self._ids(payload, "orden_bordado"))

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, "OB-000")
        self.assertIn(self.b["orden_bordado"].pk, self._ids(payload, "orden_bordado"))

    def test_no_admin_no_ve_ob_fuera_de_sus_sucursales(self):
        user = self._usuario_con("suc", ["R-PRODUCCION-OB"])
        payload = self._buscar(user, "OB-000")
        self.assertEqual(self._ids(payload, "orden_bordado"), [self.a["orden_bordado"].pk])

    def test_no_admin_ve_la_sucursal_que_le_da_el_m2m(self):
        user = self._usuario_con("m2m", ["R-PRODUCCION-OB"])
        user.sucursales.add(self.sucursal_a2)
        payload = self._buscar(user, "OB-000")
        self.assertEqual(
            sorted(self._ids(payload, "orden_bordado")),
            sorted([self.a["orden_bordado"].pk, self.ob_otra_sucursal.pk]),
        )

    def test_ob_con_soft_delete_no_aparece(self):
        self.a["orden_bordado"].soft_delete()
        payload = self._buscar(self.a["admin"], "OB-000")
        self.assertEqual(self._ids(payload, "orden_bordado"), [self.ob_otra_sucursal.pk])

    # --- coincidencia ---------------------------------------------------------

    def test_folio_coincide_por_subcadena(self):
        """El folio es NOMBRE (``icontains``): el año va delante, así que lo que la
        gente teclea —``OB-00027``, ``00027``— está en medio del folio."""
        for q in ("2026-OB", "2026-ob-00027", "OB-00027", "ob-00027", "00027"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertIn(self.a["orden_bordado"].pk, self._ids(payload, "orden_bordado"))
        payload = self._buscar(self.a["admin"], "00027")
        self.assertEqual(self._ids(payload, "orden_bordado"), [self.a["orden_bordado"].pk])

    def test_q_de_2_caracteres_no_devuelve_ordenes(self):
        """Sin campos CÓDIGO, por debajo de 3 caracteres no se consulta nada: el
        grupo llega vacío, igual que clientes y cotizaciones."""
        payload = self._buscar(self.a["admin"], "OB")
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "orden_bordado")
        self.assertEqual(grupo["resultados"], [])
        self.assertFalse(grupo["hay_mas"])

    def test_solo_coincide_por_su_folio(self):
        """Ni el folio del pedido ni el nombre del cliente encuentran la OB."""
        for q in ("P-00027", "acme"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertEqual(self._ids(payload, "orden_bordado"), [])

    # --- forma de la fila -----------------------------------------------------

    def test_fila_lleva_folio_pedido_cliente_y_estatus_legible(self):
        payload = self._buscar(self.a["admin"], "OB-00027")
        grupo = next(g for g in payload["grupos"] if g["tipo"] == "orden_bordado")
        self.assertEqual(grupo["etiqueta"], "Órdenes de bordado")
        self.assertEqual(
            grupo["resultados"][0],
            {
                "tipo": "orden_bordado",
                "id": self.a["orden_bordado"].pk,
                "codigo": "2026-OB-00027",
                "titulo": "2026-OB-00027",
                "subtitulo": f"P-00027 · {self.a['cliente'].razon_social}",
                "estatus": "Sin trabajar",
            },
        )

    def test_subtitulo_degrada_si_el_pedido_no_tiene_folio(self):
        self.a["pedido"].folio = None
        self.a["pedido"].save(update_fields=["folio"])
        payload = self._buscar(self.a["admin"], "OB-00027")
        fila = next(g for g in payload["grupos"] if g["tipo"] == "orden_bordado")["resultados"][0]
        self.assertEqual(fila["subtitulo"], self.a["cliente"].razon_social)

    # --- coste ----------------------------------------------------------------

    def test_coste_no_crece_con_el_numero_de_filas(self):
        """Pedido y cliente salen del ``select_related``: sin N+1 por fila."""
        user = self._usuario_con("coste", ["R-PRODUCCION-OB"])

        def consultas():
            client = APIClient()
            client.force_authenticate(user=user)
            with CaptureQueriesContext(connection) as capturadas:
                resp = client.get(f"{SEARCH_URL}?q=OB-")
            self.assertEqual(resp.status_code, 200)
            return len(capturadas), resp.json()

        coste_1, payload = consultas()
        self.assertEqual(len(self._ids(payload, "orden_bordado")), 1)
        for i in range(3):
            OrdenesBordado.objects.create(
                empresa=self.a["empresa"],
                sucursal=self.a["sucursal"],
                pedido=self.a["pedido"],
                folio_bordado=f"2026-OB-1000{i}",
            )
        coste_4, payload = consultas()
        self.assertEqual(len(self._ids(payload, "orden_bordado")), 4)
        self.assertEqual(coste_4, coste_1)


class _OrdenTrabajoBusquedaMixin:
    """Batería de ``OrdenBordadoBusquedaTests`` para las otras órdenes de trabajo.

    Reflejante y corte de manga siguen la plantilla de la OB: alcance de su propio
    ViewSet (``produccion.scope``), folio como NOMBRE (subcadena, 3+ caracteres),
    sólo su clave de sección y la misma forma de fila. Cada subclase declara su
    modelo, su campo de folio y la serie real de su folio.
    """

    TIPO = None
    ETIQUETA = None
    MODEL = None
    FOLIO_FIELD = None
    SERIE = None  # "OR" / "CM": ``<año>-<serie>-<consecutivo>``, como en producción.
    CLAVE = None
    ESTATUS_INICIAL = "Pendiente"

    @classmethod
    def _orden(cls, tenant, folio, sucursal=None):
        return cls.MODEL.objects.create(
            empresa=tenant["empresa"],
            sucursal=sucursal or tenant["sucursal"],
            pedido=tenant["pedido"],
            **{cls.FOLIO_FIELD: folio},
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Mismo consecutivo base en las dos empresas: ``<SERIE>-000`` está en el
        # folio de ambas, así que una fuga de aislamiento se vería al buscarlo.
        cls.orden_a = cls._orden(cls.a, f"2026-{cls.SERIE}-00027")
        cls.orden_b = cls._orden(cls.b, f"2026-{cls.SERIE}-00028")
        # Segunda sucursal de la empresa A, a la que los usuarios no admin de
        # abajo no tienen acceso salvo que se les dé por el M2M.
        cls.sucursal_a2 = Sucursal.objects.create(
            empresa=cls.a["empresa"], codigo="AC2", nombre="acme-search 2"
        )
        cls.orden_otra_sucursal = cls._orden(
            cls.a, f"2026-{cls.SERIE}-00029", sucursal=cls.sucursal_a2
        )

    def _usuario_con(self, sufijo, claves=()):
        user = Usuario.objects.create(
            username=f"{self.TIPO}-{sufijo}",
            email=f"{self.TIPO}-{sufijo}@acme-search.test",
            empresa=self.a["empresa"],
            sucursal_default=self.a["sucursal"],
        )
        if claves:
            rol = self._rol_con(self.a["empresa"], f"rol-{self.TIPO}-{sufijo}", claves)
            UsuarioRol.objects.create(usuario=user, rol=rol, empresa=self.a["empresa"])
        return user

    def _tipos(self, user, q=None):
        return [g["tipo"] for g in self._buscar(user, q or f"{self.SERIE}-")["grupos"]]

    def _grupo(self, payload):
        return next(g for g in payload["grupos"] if g["tipo"] == self.TIPO)

    # --- visibilidad ----------------------------------------------------------

    def test_codigo_de_seccion_basta(self):
        self.assertIn(self.TIPO, self._tipos(self._usuario_con("sec", [self.CLAVE])))

    def test_codigo_de_modulo_solo_no_basta(self):
        """``R-PRODUCCION`` no abre la sección de esta orden en el frontend."""
        self.assertNotIn(self.TIPO, self._tipos(self._usuario_con("mod", ["R-PRODUCCION"])))

    def test_sin_el_codigo_de_seccion_se_omite(self):
        otras = [
            c for c in ("R-PRODUCCION-OB", "R-PRODUCCION-OR", "R-PRODUCCION-CM")
            if c != self.CLAVE
        ]
        user = self._usuario_con("otros", otras + ["R-CRM-PEDIDOS"])
        self.assertNotIn(self.TIPO, self._tipos(user))

    def test_deny_oculta_la_entidad_si_nada_mas_la_concede(self):
        user = self._usuario_con("deny", [self.CLAVE])
        self.assertIn(self.TIPO, self._tipos(user))
        UsuarioPermiso.objects.create(
            usuario=user,
            permiso=Permiso.objects.get(clave=self.CLAVE),
            tipo=UsuarioPermiso.TIPO_DENY,
        )
        self.assertNotIn(self.TIPO, self._tipos(user))

    # --- alcance de fila ------------------------------------------------------

    def test_orden_de_otra_empresa_no_aparece(self):
        payload = self._buscar(self.a["admin"], f"{self.SERIE}-000")
        self.assertEqual(
            sorted(self._ids(payload, self.TIPO)),
            sorted([self.orden_a.pk, self.orden_otra_sucursal.pk]),
        )

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, f"{self.SERIE}-000")
        self.assertEqual(
            sorted(self._ids(payload, self.TIPO)),
            sorted([self.orden_a.pk, self.orden_b.pk, self.orden_otra_sucursal.pk]),
        )

    def test_no_admin_no_ve_ordenes_fuera_de_sus_sucursales(self):
        user = self._usuario_con("suc", [self.CLAVE])
        payload = self._buscar(user, f"{self.SERIE}-000")
        self.assertEqual(self._ids(payload, self.TIPO), [self.orden_a.pk])

    def test_no_admin_ve_la_sucursal_que_le_da_el_m2m(self):
        user = self._usuario_con("m2m", [self.CLAVE])
        user.sucursales.add(self.sucursal_a2)
        payload = self._buscar(user, f"{self.SERIE}-000")
        self.assertEqual(
            sorted(self._ids(payload, self.TIPO)),
            sorted([self.orden_a.pk, self.orden_otra_sucursal.pk]),
        )

    def test_orden_con_soft_delete_no_aparece(self):
        self.orden_a.soft_delete()
        payload = self._buscar(self.a["admin"], f"{self.SERIE}-000")
        self.assertEqual(self._ids(payload, self.TIPO), [self.orden_otra_sucursal.pk])

    # --- coincidencia ---------------------------------------------------------

    def test_folio_coincide_por_subcadena(self):
        """El año va delante del folio: lo que se teclea está en medio."""
        serie = self.SERIE
        for q in (f"2026-{serie}", f"{serie}-00027", f"{serie.lower()}-00027", "00027"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertIn(self.orden_a.pk, self._ids(payload, self.TIPO))
        payload = self._buscar(self.a["admin"], "00027")
        self.assertEqual(self._ids(payload, self.TIPO), [self.orden_a.pk])

    def test_q_de_2_caracteres_no_devuelve_ordenes(self):
        grupo = self._grupo(self._buscar(self.a["admin"], self.SERIE))
        self.assertEqual(grupo["resultados"], [])
        self.assertFalse(grupo["hay_mas"])

    def test_solo_coincide_por_su_folio(self):
        """Ni el folio del pedido ni el nombre del cliente encuentran la orden."""
        for q in ("P-00027", "acme"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertEqual(self._ids(payload, self.TIPO), [])

    # --- forma de la fila -----------------------------------------------------

    def test_fila_lleva_folio_pedido_cliente_y_estatus_legible(self):
        folio = f"2026-{self.SERIE}-00027"
        grupo = self._grupo(self._buscar(self.a["admin"], folio))
        self.assertEqual(grupo["etiqueta"], self.ETIQUETA)
        self.assertEqual(
            grupo["resultados"][0],
            {
                "tipo": self.TIPO,
                "id": self.orden_a.pk,
                "codigo": folio,
                "titulo": folio,
                "subtitulo": f"P-00027 · {self.a['cliente'].razon_social}",
                "estatus": self.ESTATUS_INICIAL,
            },
        )

    def test_subtitulo_degrada_si_el_pedido_no_tiene_folio(self):
        self.a["pedido"].folio = None
        self.a["pedido"].save(update_fields=["folio"])
        grupo = self._grupo(self._buscar(self.a["admin"], f"{self.SERIE}-00027"))
        self.assertEqual(grupo["resultados"][0]["subtitulo"], self.a["cliente"].razon_social)

    # --- coste ----------------------------------------------------------------

    def test_coste_no_crece_con_el_numero_de_filas(self):
        """Pedido y cliente salen del ``select_related``: sin N+1 por fila."""
        user = self._usuario_con("coste", [self.CLAVE])

        def consultas():
            client = APIClient()
            client.force_authenticate(user=user)
            with CaptureQueriesContext(connection) as capturadas:
                resp = client.get(f"{SEARCH_URL}?q={self.SERIE}-")
            self.assertEqual(resp.status_code, 200)
            return len(capturadas), resp.json()

        coste_1, payload = consultas()
        self.assertEqual(len(self._ids(payload, self.TIPO)), 1)
        for i in range(3):
            self._orden(self.a, f"2026-{self.SERIE}-1000{i}")
        coste_4, payload = consultas()
        self.assertEqual(len(self._ids(payload, self.TIPO)), 4)
        self.assertEqual(coste_4, coste_1)


class OrdenReflejanteBusquedaTests(_OrdenTrabajoBusquedaMixin, BusquedaGlobalBaseTestCase):
    """Órdenes de reflejante: alcance de ``OrdenReflejanteViewSet``."""

    TIPO = "orden_reflejante"
    ETIQUETA = "Órdenes de reflejante"
    MODEL = OrdenesReflejante
    FOLIO_FIELD = "folio_reflejante"
    SERIE = "OR"
    CLAVE = "R-PRODUCCION-OR"


class _GrupoPropioMixin:
    """Atajos compartidos por las suites de una sola entidad."""

    #: ``tipo`` del grupo que mide la suite.
    TIPO = None

    def _usuario_con(self, sufijo, claves=(), sucursal=None):
        user = Usuario.objects.create(
            username=f"{self.TIPO}-{sufijo}",
            email=f"{self.TIPO}-{sufijo}@acme-search.test",
            empresa=self.a["empresa"],
            sucursal_default=sucursal or self.a["sucursal"],
        )
        if claves:
            rol = self._rol_con(self.a["empresa"], f"rol-{self.TIPO}-{sufijo}", claves)
            UsuarioRol.objects.create(usuario=user, rol=rol, empresa=self.a["empresa"])
        return user

    def _tipos(self, user, q):
        return [g["tipo"] for g in self._buscar(user, q)["grupos"]]

    def _grupo(self, payload):
        return next(g for g in payload["grupos"] if g["tipo"] == self.TIPO)

    def _deny(self, user, clave):
        UsuarioPermiso.objects.create(
            usuario=user,
            permiso=Permiso.objects.get(clave=clave),
            tipo=UsuarioPermiso.TIPO_DENY,
        )

    def _coste(self, user, q):
        client = APIClient()
        client.force_authenticate(user=user)
        with CaptureQueriesContext(connection) as capturadas:
            resp = client.get(f"{SEARCH_URL}?q={q}")
        self.assertEqual(resp.status_code, 200)
        return len(capturadas), resp.json()


class FacturaBusquedaTests(_GrupoPropioMixin, BusquedaGlobalBaseTestCase):
    """Facturas: visibilidad, alcance de fila y forma de la fila.

    El alcance es el de ``FacturaViewSet`` (``_aplicar_scope_empresa``: empresa,
    sin sub-alcance por sucursal ni vendedor) más ``activo=True``.
    """

    TIPO = "factura"

    @classmethod
    def _factura(cls, tenant, folio, **extra):
        return Factura.objects.create(
            empresa=tenant["empresa"],
            sucursal=tenant["sucursal"],
            cliente=tenant["cliente"],
            moneda=cls.moneda,
            folio=folio,
            **extra,
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Formato real: ``FAC-<consecutivo>``. ``FAC-000`` es prefijo de las dos
        # empresas: una fuga de aislamiento se vería al buscarlo.
        cls.fac_a = cls._factura(cls.a, "FAC-00027")
        cls.fac_b = cls._factura(cls.b, "FAC-00028")

    # --- visibilidad ----------------------------------------------------------

    def test_codigo_de_seccion_basta(self):
        user = self._usuario_con("sec", ["R-CONTABILIDAD-FACTURACION"])
        self.assertIn("factura", self._tipos(user, "FAC"))

    def test_codigo_de_modulo_solo_no_basta(self):
        """``R-CONTABILIDAD`` no abre la sección de facturación en el frontend."""
        user = self._usuario_con("mod", ["R-CONTABILIDAD"])
        self.assertNotIn("factura", self._tipos(user, "FAC"))

    def test_sin_el_codigo_de_seccion_se_omite(self):
        # Ni la sección de clientes de Contabilidad ni la de CxC conceden facturas.
        user = self._usuario_con(
            "otros", ["R-CONTABILIDAD-CLIENTES", "R-CONTABILIDAD-CXC", "R-CRM-PEDIDOS"]
        )
        self.assertNotIn("factura", self._tipos(user, "FAC"))

    def test_deny_oculta_la_entidad_si_nada_mas_la_concede(self):
        user = self._usuario_con("deny", ["R-CONTABILIDAD-FACTURACION"])
        self.assertIn("factura", self._tipos(user, "FAC"))
        self._deny(user, "R-CONTABILIDAD-FACTURACION")
        self.assertNotIn("factura", self._tipos(user, "FAC"))

    # --- alcance de fila ------------------------------------------------------

    def test_factura_de_otra_empresa_no_aparece(self):
        user = self._usuario_con("emp", ["R-CONTABILIDAD-FACTURACION"])
        payload = self._buscar(user, "FAC-000")
        self.assertEqual(self._ids(payload, "factura"), [self.fac_a.pk])

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, "FAC-000")
        self.assertEqual(
            sorted(self._ids(payload, "factura")), sorted([self.fac_a.pk, self.fac_b.pk])
        )

    def test_no_hay_sub_alcance_por_sucursal(self):
        """Igual que ``FacturaViewSet``: el alcance es la empresa, no la sucursal."""
        otra = Sucursal.objects.create(
            empresa=self.a["empresa"], codigo="AC3", nombre="acme-search 3"
        )
        user = self._usuario_con("suc", ["R-CONTABILIDAD-FACTURACION"], sucursal=otra)
        payload = self._buscar(user, "FAC-00027")
        self.assertEqual(self._ids(payload, "factura"), [self.fac_a.pk])

    def test_factura_con_soft_delete_no_aparece(self):
        self.fac_a.soft_delete()
        payload = self._buscar(self.a["admin"], "FAC-000")
        self.assertEqual(self._ids(payload, "factura"), [])

    # --- coincidencia ---------------------------------------------------------

    def test_folio_coincide_por_prefijo_no_por_subcadena(self):
        for q in ("FAC", "fac-00027", "FAC-00027"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertEqual(self._ids(payload, "factura"), [self.fac_a.pk])
        payload = self._buscar(self.a["admin"], "00027")
        self.assertEqual(self._ids(payload, "factura"), [])

    def test_coincide_por_nombre_y_razon_social_del_cliente(self):
        for q in ("comercial acme norte", "ACME NORTE SA DE CV"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertEqual(self._ids(payload, "factura"), [self.fac_a.pk])

    def test_factura_sin_folio_se_encuentra_por_cliente(self):
        sin_folio = self._factura(self.a, None)
        payload = self._buscar(self.a["admin"], "acme norte")
        self.assertIn(sin_folio.pk, self._ids(payload, "factura"))
        fila = next(
            f for f in self._grupo(payload)["resultados"] if f["id"] == sin_folio.pk
        )
        self.assertIsNone(fila["codigo"])
        self.assertEqual(fila["titulo"], self.a["cliente"].razon_social)
        self.assertIsNone(fila["subtitulo"])

    def test_q_de_2_caracteres_va_solo_por_folio(self):
        payload = self._buscar(self.a["admin"], "FA")
        self.assertEqual(self._ids(payload, "factura"), [self.fac_a.pk])
        payload = self._buscar(self.a["admin"], "Ac")
        self.assertEqual(self._ids(payload, "factura"), [])

    # --- forma de la fila -----------------------------------------------------

    def test_fila_lleva_folio_cliente_y_estatus_legible(self):
        payload = self._buscar(self.a["admin"], "FAC-00027")
        grupo = self._grupo(payload)
        self.assertEqual(grupo["etiqueta"], "Facturas")
        self.assertEqual(
            grupo["resultados"][0],
            {
                "tipo": "factura",
                "id": self.fac_a.pk,
                "codigo": "FAC-00027",
                "titulo": "FAC-00027",
                "subtitulo": self.a["cliente"].razon_social,
                "estatus": "Borrador",
            },
        )

    # --- coste ----------------------------------------------------------------

    def test_coste_no_crece_con_el_numero_de_filas(self):
        """El cliente sale del ``select_related``: sin N+1 por fila."""
        user = self._usuario_con("coste", ["R-CONTABILIDAD-FACTURACION"])
        coste_1, payload = self._coste(user, "FAC")
        self.assertEqual(len(self._ids(payload, "factura")), 1)
        for i in range(3):
            self._factura(self.a, f"FAC-1000{i}")
        coste_4, payload = self._coste(user, "FAC")
        self.assertEqual(len(self._ids(payload, "factura")), 4)
        self.assertEqual(coste_4, coste_1)


class ProductoBusquedaTests(_GrupoPropioMixin, BusquedaGlobalBaseTestCase):
    """Productos (``catalogo.ProductoVariante``): visibilidad, alcance y forma.

    El alcance es el de ``ProductoVarianteViewSet`` (``_alcance_empresa``: empresa,
    sin sub-alcance por sucursal) más ``activo=True`` sobre la variante.

    ``R-CATALOGO-PRODUCTOS`` NO existe todavía en el catálogo real de permisos; aquí
    se siembra en la BD de pruebas, como cualquier otra clave (``_rol_con``).
    """

    TIPO = "producto"
    CLAVE = "R-CATALOGO-PRODUCTOS"

    @classmethod
    def _variante(cls, tenant, producto, sku, **extra):
        return ProductoVariante.objects.create(
            producto=producto,
            empresa=tenant["empresa"],
            color=cls.color,
            talla=cls.talla,
            sku=sku,
            precio_base="100.00",
            **extra,
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.color = Color.objects.create(nombre="Marino", codigo="MAR", codigo_hex="#000080")
        cls.talla = Talla.objects.create(nombre="XG")
        # Mismo nombre de producto y mismo prefijo de SKU en las dos empresas: una
        # fuga de aislamiento se vería al buscar cualquiera de los dos. Formato de
        # SKU real: modelo + color + talla concatenados (``10808015XG``).
        cls.prod_a = Producto.objects.create(empresa=cls.a["empresa"], nombre="Camisola ignifuga")
        cls.prod_b = Producto.objects.create(empresa=cls.b["empresa"], nombre="Camisola ignifuga")
        cls.var_a = cls._variante(cls.a, cls.prod_a, "1080801XG")
        cls.var_b = cls._variante(cls.b, cls.prod_b, "1080802XG")

    # --- visibilidad ----------------------------------------------------------

    def test_la_clave_de_productos_basta(self):
        self.assertIn("producto", self._tipos(self._usuario_con("sec", [self.CLAVE]), "10808"))

    def test_sin_la_clave_se_omite(self):
        user = self._usuario_con("otros", ["R-WMS-EXISTENCIAS", "R-WMS", "R-COMPRAS"])
        self.assertNotIn("producto", self._tipos(user, "10808"))

    def test_deny_oculta_la_entidad(self):
        user = self._usuario_con("deny", [self.CLAVE])
        self.assertIn("producto", self._tipos(user, "10808"))
        self._deny(user, self.CLAVE)
        self.assertNotIn("producto", self._tipos(user, "10808"))

    # --- alcance de fila ------------------------------------------------------

    def test_producto_de_otra_empresa_no_aparece(self):
        user = self._usuario_con("emp", [self.CLAVE])
        for q in ("10808", "camisola"):
            with self.subTest(q=q):
                payload = self._buscar(user, q)
                self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])

    def test_superuser_ve_las_dos_empresas(self):
        payload = self._buscar(self.superuser, "10808")
        self.assertEqual(
            sorted(self._ids(payload, "producto")), sorted([self.var_a.pk, self.var_b.pk])
        )

    def test_no_hay_sub_alcance_por_sucursal(self):
        """Igual que ``ProductoVarianteViewSet``: el alcance es la empresa."""
        otra = Sucursal.objects.create(
            empresa=self.a["empresa"], codigo="AC4", nombre="acme-search 4"
        )
        user = self._usuario_con("suc", [self.CLAVE], sucursal=otra)
        payload = self._buscar(user, "1080801")
        self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])

    def test_variante_inactiva_no_aparece(self):
        ProductoVariante.objects.filter(pk=self.var_a.pk).update(activo=False)
        payload = self._buscar(self.a["admin"], "10808")
        self.assertEqual(self._ids(payload, "producto"), [])

    # --- coincidencia ---------------------------------------------------------

    def test_sku_coincide_por_prefijo_no_por_subcadena(self):
        for q in ("10808", "1080801xg", "1080801XG"):
            with self.subTest(q=q):
                payload = self._buscar(self.a["admin"], q)
                self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])
        payload = self._buscar(self.a["admin"], "0801XG")
        self.assertEqual(self._ids(payload, "producto"), [])

    def test_coincide_por_nombre_de_la_variante(self):
        # ``ProductoVariante.save()`` arma ``nombre`` como "producto - color - talla":
        # el color sólo está en el nombre de la variante, no en el del producto.
        payload = self._buscar(self.a["admin"], "marino")
        self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])

    def test_coincide_por_nombre_del_producto(self):
        # Se vacía el nombre de la variante (sin pasar por ``save()``) para que sólo
        # pueda coincidir por ``producto__nombre``.
        ProductoVariante.objects.filter(pk=self.var_a.pk).update(nombre="")
        payload = self._buscar(self.a["admin"], "ignifuga")
        self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])

    def test_q_de_2_caracteres_va_solo_por_sku(self):
        payload = self._buscar(self.a["admin"], "10")
        self.assertEqual(self._ids(payload, "producto"), [self.var_a.pk])
        payload = self._buscar(self.a["admin"], "Ca")
        self.assertEqual(self._ids(payload, "producto"), [])

    # --- forma de la fila -----------------------------------------------------

    def test_fila_lleva_sku_y_nombre_del_producto(self):
        payload = self._buscar(self.a["admin"], "1080801XG")
        grupo = self._grupo(payload)
        self.assertEqual(grupo["etiqueta"], "Productos")
        self.assertEqual(
            grupo["resultados"][0],
            {
                "tipo": "producto",
                "id": self.var_a.pk,
                "codigo": "1080801XG",
                "titulo": "1080801XG",
                "subtitulo": "Camisola ignifuga",
                "estatus": None,
            },
        )

    # --- coste ----------------------------------------------------------------

    def test_coste_no_crece_con_el_numero_de_filas(self):
        """El producto sale del ``select_related``: sin N+1 por fila."""
        user = self._usuario_con("coste", [self.CLAVE])
        coste_1, payload = self._coste(user, "10808")
        self.assertEqual(len(self._ids(payload, "producto")), 1)
        for i in range(3):
            self._variante(self.a, self.prod_a, f"1080810{i}XG")
        coste_4, payload = self._coste(user, "10808")
        self.assertEqual(len(self._ids(payload, "producto")), 4)
        self.assertEqual(coste_4, coste_1)


class CotizacionQFiltroNoRegresionTests(BusquedaGlobalBaseTestCase):
    """El filtro ``?q=`` de cotizaciones no cambió al extraer el aislamiento."""

    def _cotizaciones(self, user, query=""):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"{COTIZACIONES_URL}{query}")

    def test_admin_sigue_viendo_solo_su_empresa(self):
        resp = self._cotizaciones(self.a["admin"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cotizacion"].pk])

    def test_filtro_q_por_cliente_sigue_funcionando(self):
        resp = self._cotizaciones(self.a["admin"], "?q=ACME NORTE")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cotizacion"].pk])

    def test_filtro_q_no_cruza_empresas(self):
        resp = self._cotizaciones(self.a["admin"], "?q=ACME SUR")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_usuario_sin_empresa_con_estatus_invalido_sigue_devolviendo_200(self):
        """Sale antes de ``_apply_filters``: antes daba 200 [] y debe seguir igual."""
        resp = self._cotizaciones(self.sin_empresa, "?estatus=abc")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_estatus_invalido_sigue_siendo_400_para_quien_si_tiene_alcance(self):
        resp = self._cotizaciones(self.a["admin"], "?estatus=abc")
        self.assertEqual(resp.status_code, 400)


class MesaControlUsaElAlcanceCanonicoTests(BusquedaGlobalBaseTestCase):
    """Los ViewSets de mesa de control ya no llevan su propia copia del alcance."""

    def _get(self, user, url):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url)

    def test_clientes_mesa_control_ya_no_sirve_borrados(self):
        """Antes reconstruía desde ``Cliente.objects`` y perdía ``activo=True``."""
        self.a["cliente"].soft_delete()
        resp = self._get(self.a["admin"], "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [])

    def test_clientes_mesa_control_sigue_acotado_a_su_empresa(self):
        resp = self._get(self.a["admin"], "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["id"] for r in resp.json()], [self.a["cliente"].pk])

    def test_clientes_mesa_control_no_da_nada_a_usuario_sin_empresa(self):
        resp = self._get(self.sin_empresa, "/api/v1/terceros/clientes-mesa-control/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class HealthChecksTests(TestCase):
    """``/healthz/`` (liveness) y ``/readyz/`` (readiness, valida DB). Sin
    auth: son para monitoreo externo, no para un usuario logueado."""

    def test_healthz_no_toca_la_db(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = APIClient().get("/healthz/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_readyz_ok_con_db_disponible(self):
        resp = APIClient().get("/readyz/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["checks"]["database"]["ok"])
        self.assertIn("latency_ms", body["checks"]["database"])

    def test_readyz_503_si_la_db_falla(self):
        # ``connection`` (default alias) y ``connections["default"]`` resuelven
        # al mismo objeto subyacente -- se parchea el método directamente en
        # vez de reconstruir la cadena de mocks de ``connections`` a mano.
        with patch.object(connection, "cursor", side_effect=Exception("conexión rechazada")):
            resp = APIClient().get("/readyz/")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["checks"]["database"]["ok"])
        self.assertIn("conexión rechazada", body["checks"]["database"]["error"])
