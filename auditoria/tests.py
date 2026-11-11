"""Comportamiento esperado de la bitácora de documentos (#1062-#1065).

Documentación ejecutable: NO correr ``manage.py test`` (ver CLAUDE.md, pruebas
en producción con rollback).
"""
from django.test import TestCase

from auditoria.bitacora import DOCUMENTOS, TIPOS, etiqueta_uniforme


class EtiquetasTests(TestCase):
    def test_formato_oracion_y_acentos(self):
        self.assertEqual(etiqueta_uniforme("POR AUTORIZAR"), "Por autorizar")
        self.assertEqual(etiqueta_uniforme("Preparacion"), "Preparación")
        self.assertEqual(etiqueta_uniforme("Cancelado (legacy)"), "Cancelado")
        self.assertEqual(etiqueta_uniforme("EN_PROCESO"), "En proceso")
        self.assertEqual(etiqueta_uniforme(None), "")

    def test_tipos_unicos(self):
        self.assertEqual(len(TIPOS), len(DOCUMENTOS))


class ApiBitacoraEsperado(TestCase):
    """Contrato de ``/api/v1/auditoria/documentos/``:

    - 403 sin ``R-CORE`` ni ``R-CORE-REPORTES`` (superusuario y admin pasan).
    - Solo la empresa activa; no admins, solo sus ``sucursales_permitidas()``.
    - ``total`` es ``null`` sin permiso de importes.
    - ``?usuario=abc``, ``?fecha_desde=2026-13-01``, ``?tipo=xyz``, ``?accion=x`` → 400.
    - ``?ordering=`` y ``?search=`` se ignoran: el orden es siempre ``-id``.
    - ``linea-tiempo``: 400 sin ``tipo`` válido o ``documento_id``; 404 si el
      documento no es de la empresa; ``{"eventos", "truncado"}`` con los 500
      eventos más recientes en orden cronológico, incluyendo derivados
      (OC → recepción, picking → transferencia, factura → cobro).
    - Cada ``save()`` que cambia el estatus agrega una fila; uno sin cambios, ninguna.
    """
