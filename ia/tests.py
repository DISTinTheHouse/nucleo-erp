import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from ia.api.views import AIAssistantAPIView
from ventas.tests import ImportesPedidoFixture

CHAT_URL = "/api/v1/ai/chat/"


class ChatUltimaCompraMixin:
    """Corre la herramienta ``get_last_cliente_compra`` por ``/ai/chat/`` con un modelo
    falso. Va con ``ImportesPedidoFixture`` (que da ``_api``) y ``TestCase``."""

    def _chat(self, usuario):
        """Corre ``/ai/chat/`` con un modelo falso que pide la herramienta y luego
        responde; devuelve la respuesta y lo que se le mandó al modelo."""
        enviados = []
        respuestas = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "get_last_cliente_compra", "arguments": "{}"},
            }]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "listo"}}]},
        ])

        def falso(_self, base_url, api_key, model, messages, tools):
            enviados.append(json.loads(json.dumps(messages)))
            return next(respuestas)

        api = self._api(usuario)
        with override_settings(OPENAI_API_KEY="sk-test"), patch.object(AIAssistantAPIView, "_openai_chat", falso):
            resp = api.post(CHAT_URL, {"message": "¿Quién fue mi último cliente?"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        al_modelo = json.loads(next(m for m in enviados[-1] if m.get("role") == "tool")["content"])
        return resp.data["tool_results"][0]["result"], al_modelo


class UltimaCompraImportesPorPerfilTests(ChatUltimaCompraMixin, ImportesPedidoFixture, TestCase):
    """EC-784: la herramienta ``get_last_cliente_compra`` del asistente sigue la regla
    de importes del pedido (contabilidad de ventas O de compras). Sin ella omite
    ``last_purchase.gran_total`` tanto en lo que se manda al modelo (mensaje
    ``tool``) como en ``tool_results``; trae ``puede_ver_importes``."""

    def test_ultima_compra_por_perfil(self):
        for usuario, ve in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                resultado, al_modelo = self._chat(usuario)
                for datos in (resultado, al_modelo):
                    compra = datos["last_purchase"]
                    # Cada perfil es vendedor de su propio pedido: es su última compra.
                    self.assertEqual(compra["pedido_id"], self.pedidos[usuario.pk].pk)
                    self.assertEqual("gran_total" in compra, ve)
                    self.assertIn("folio", compra)
                    self.assertIs(datos.get("puede_ver_importes"), ve)

    def test_superusuario_ve_el_importe(self):
        resultado, al_modelo = self._chat(self.superusuario)
        for datos in (resultado, al_modelo):
            self.assertIn("gran_total", datos["last_purchase"])
            self.assertIs(datos["puede_ver_importes"], True)

    def test_no_cruza_de_empresa(self):
        """Para cada perfil hay un pedido MÁS NUEVO en la otra empresa del que es
        vendedor (``cotizacion.vendedor``): sin el filtro por empresa sería su
        "última compra". Con el filtro, sigue siendo el pedido de su empresa."""
        for usuario, _ in self.perfiles[:-1]:
            with self.subTest(usuario=usuario.username):
                ajeno = self._pedido(
                    self.otra, self.sucursal_otra, self.cliente_otra, usuario,
                    f"EC784-AJENO-{usuario.pk}", "777.00",
                )
                resultado, al_modelo = self._chat(usuario)
                for datos in (resultado, al_modelo):
                    self.assertEqual(datos["last_purchase"]["pedido_id"], self.pedidos[usuario.pk].pk)
                    self.assertNotEqual(datos["last_purchase"]["pedido_id"], ajeno.pk)


class UltimaCompraVentaRealTests(ChatUltimaCompraMixin, ImportesPedidoFixture, TestCase):
    """EC-830: ``get_last_cliente_compra`` usa la misma regla de compra que
    ``fecha_ultima_compra`` (``ventas.scope.pedidos_venta_real``): un pedido MÁS
    NUEVO que sea MUESTRA, PEDIDO DE ERROR, BORRADOR, POR AUTORIZAR o CANCELADO no
    es la última compra. Alcance de ``ventas.scope.pedidos_visibles``: sólo la empresa
    activa, superusuario incluido (EC-839); el vendedor, sólo lo suyo."""

    def _nuevo(self, empresa, sucursal, cliente, folio, vendedor=None, **campos):
        from ventas.models import Pedido

        pedido = self._pedido(empresa, sucursal, cliente, vendedor or self.admin_empresa, folio, "1.00")
        if campos:
            Pedido.objects.filter(pk=pedido.pk).update(**campos)
        return pedido

    def _ultima(self, usuario=None):
        resultado, al_modelo = self._chat(usuario or self.admin_empresa)
        self.assertEqual(resultado["last_purchase"], al_modelo["last_purchase"])
        return resultado["last_purchase"]

    def test_excluidos_mas_recientes_no_son_la_ultima_compra(self):
        from ventas.models import TIPO_PEDIDO_ERROR, TIPO_PEDIDO_MUESTRA, Pedido

        real = self._nuevo(self.empresa, self.sucursal, self.cliente, "EC830-REAL")
        casos = {
            "MUESTRA": {"tipo_pedido": TIPO_PEDIDO_MUESTRA},
            "PEDIDO DE ERROR": {"tipo_pedido": TIPO_PEDIDO_ERROR},
            "BORRADOR": {"estatus": Pedido.ESTATUS_BORRADOR},
            "POR AUTORIZAR": {"estatus": Pedido.ESTATUS_POR_AUTORIZAR},
            "CANCELADO": {"estatus": Pedido.ESTATUS_CANCELADO},
        }
        for caso, campos in casos.items():
            with self.subTest(caso=caso):
                excluido = self._nuevo(self.empresa, self.sucursal, self.cliente, f"EC830-{caso}", **campos)
                self.assertEqual(self._ultima()["pedido_id"], real.pk)
                excluido.delete()

    def test_en_proceso_si_cuenta(self):
        from ventas.models import Pedido

        en_proceso = self._nuevo(
            self.empresa, self.sucursal, self.cliente, "EC830-EP", estatus=Pedido.ESTATUS_EN_PROCESO,
        )
        self.assertEqual(self._ultima()["pedido_id"], en_proceso.pk)

    def test_superusuario_solo_su_empresa_activa(self):
        """EC-839: antes el superusuario veía la última venta de CUALQUIER empresa."""
        propio = self._nuevo(self.empresa, self.sucursal, self.cliente, "EC839-PROPIO")
        ajeno = self._nuevo(self.otra, self.sucursal_otra, self.cliente_otra, "EC839-AJENO")
        compra = self._ultima(self.superusuario)
        self.assertEqual(compra["pedido_id"], propio.pk)
        self.assertNotEqual(compra["pedido_id"], ajeno.pk)
        # Sin empresa activa no ve nada (se falla cerrado, como ``pedidos_visibles``).
        self.assertIsNone(self._ultima(self.superusuario_sin_empresa))

    def test_vendedor_solo_lo_suyo_y_solo_ventas_reales(self):
        from ventas.models import TIPO_PEDIDO_MUESTRA

        suyo = self.pedidos[self.ventas.pk]
        # Venta real más nueva de OTRO vendedor, del mismo cliente (sin vendedores
        # asignados): el vendedor no la ve.
        self._nuevo(self.empresa, self.sucursal, self.cliente, "EC830-OTRO-VEND")
        # Muestra más nueva del propio vendedor: no es compra.
        self._nuevo(
            self.empresa, self.sucursal, self.cliente, "EC830-MUESTRA-VEND",
            vendedor=self.ventas, tipo_pedido=TIPO_PEDIDO_MUESTRA,
        )
        self.assertEqual(self._ultima(self.ventas)["pedido_id"], suyo.pk)
        # Una venta real suya más nueva sí es su última compra.
        nueva = self._nuevo(self.empresa, self.sucursal, self.cliente, "EC830-NUEVA-VEND", vendedor=self.ventas)
        self.assertEqual(self._ultima(self.ventas)["pedido_id"], nueva.pk)

    def test_venta_real_de_otra_empresa_no_cruza(self):
        ajeno = self._nuevo(self.otra, self.sucursal_otra, self.cliente_otra, "EC830-AJENO")
        compra = self._ultima()
        self.assertNotEqual(compra["pedido_id"], ajeno.pk)
        self.assertEqual(compra["cliente"]["id"], self.cliente.pk)

    def test_solo_excluidos_es_null(self):
        from ventas.models import Pedido

        Pedido.objects.filter(empresa=self.empresa).update(estatus=Pedido.ESTATUS_BORRADOR)
        self.assertIsNone(self._ultima())
