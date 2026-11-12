from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from catalogo.models import ProductoVariante
from finanzas.scope import sucursales_visibles
from nucleo.models import Sucursal
from nucleo.permisos import CODIGOS_PLANEACION, RequiereCodigo
from planeacion.services import capacidad
from planeacion.services import tablero_rotacion as tablero
from ventas.services.pedido_field_filter_service import puede_ver_contabilidad


class FiltrosPlaneacionSerializer(serializers.Serializer):
    """Filtros comunes del tablero y de las necesidades de materia prima."""

    semanas = serializers.IntegerField(min_value=1, max_value=104, default=12)
    semanas_objetivo = serializers.IntegerField(min_value=1, max_value=52, default=8)
    dias_sin_venta = serializers.IntegerField(min_value=1, max_value=730, default=90)
    agrupar = serializers.ChoiceField(choices=("variante", "producto"), default="variante")
    producto = serializers.IntegerField(min_value=1, required=False)
    categoria = serializers.IntegerField(min_value=1, required=False)
    sucursal = serializers.IntegerField(min_value=1, required=False)
    buscar = serializers.CharField(max_length=100, required=False, allow_blank=True)
    clase = serializers.ChoiceField(choices=("A", "B", "C"), required=False)
    estado = serializers.ChoiceField(choices=tablero.ESTADOS, required=False)
    solo_candidatos = serializers.BooleanField(required=False, default=False)
    fabricacion = serializers.ChoiceField(choices=("SIN_BOM", "ALCANZA", "NO_ALCANZA"), required=False)

    def validate_sucursal(self, value):
        ambito = self.context.get("sucursales_visibles")
        if not Sucursal.objects.filter(pk=value, empresa=self.context["empresa"]).exists() or (
            ambito is not None and value not in ambito
        ):
            raise serializers.ValidationError("Sucursal no encontrada.")
        return value


class TableroRotacionParamsSerializer(FiltrosPlaneacionSerializer):
    ordering = serializers.CharField(required=False, default="-vendido_periodo")
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=500, default=50)

    def validate_ordering(self, value):
        if value.lstrip("-") not in tablero.ORDENAMIENTOS:
            raise serializers.ValidationError(f"Opciones: {', '.join(sorted(tablero.ORDENAMIENTOS))} (con - para descendente).")
        if value.lstrip("-") == "importe_vendido" and not self.context["ver_importes"]:
            raise serializers.ValidationError("No tienes permiso para ver importes.")
        return value

    def validate(self, attrs):
        # Con agrupar=producto no hay capacidad por variante: estos filtros/orden no aplicarían.
        if attrs["agrupar"] == "producto":
            if attrs.get("fabricacion") in ("ALCANZA", "NO_ALCANZA"):
                raise serializers.ValidationError(
                    {"fabricacion": "ALCANZA y NO_ALCANZA solo aplican con agrupar=variante."})
            if attrs["ordering"].lstrip("-") == "fabricable_con_existencia":
                raise serializers.ValidationError(
                    {"ordering": "fabricable_con_existencia solo aplica con agrupar=variante."})
        return attrs


def _ambito(user, p):
    """Ids de sucursal que acotan el cálculo: ``?sucursal=`` o, sin él, las visibles del
    usuario (``None`` = toda la empresa: superuser y admin de empresa)."""
    if p.get("sucursal"):
        return [p["sucursal"]]
    visibles = sucursales_visibles(user)
    return None if visibles is None else list(visibles)


def _params(request, serializer_class):
    """``(empresa, params, ver_importes)``; si el usuario no tiene empresa,
    ``(None, None, Response 400)``. Parámetros inválidos levantan 400."""
    empresa = getattr(request.user, "empresa", None)
    if empresa is None:
        return None, None, Response({"detail": "Tu usuario no tiene una empresa activa."}, status=400)
    ver_importes = puede_ver_contabilidad(request.user)
    # Vacíos = como si no vinieran (mismo criterio que finanzas, EC-760).
    datos = {k: v for k, v in request.query_params.items() if v != ""}
    params = serializer_class(data=datos, context={
        "ver_importes": ver_importes, "empresa": empresa, "sucursales_visibles": sucursales_visibles(request.user),
    })
    params.is_valid(raise_exception=True)
    return empresa, params.validated_data, ver_importes


class TableroRotacionView(APIView):
    """``GET /api/v1/planeacion/tablero-rotacion/``: rotación de producto terminado
    para la junta semanal (ver ``planeacion.services.tablero_rotacion``). Siempre
    sobre la empresa activa del usuario y con ``R-PRODUCCION``; los importes solo con
    permiso de importes de pedido."""

    permission_classes = [IsAuthenticated, RequiereCodigo]
    codigos_requeridos = CODIGOS_PLANEACION

    def get(self, request):
        empresa, p, ver_importes = _params(request, TableroRotacionParamsSerializer)
        if empresa is None:
            return ver_importes  # la respuesta 400
        resultado = tablero.calcular(empresa, p, _ambito(request.user, p))
        resultado.pop("boms")
        filas = resultado.pop("filas")
        if not ver_importes:
            resultado["resumen"]["importe_por_moneda"] = None
            for r in filas:
                r["importe_vendido"] = None
                r["importe_por_moneda"] = None
        inicio = (p["page"] - 1) * p["page_size"]
        return Response({
            **resultado,
            "count": len(filas),
            "page": p["page"],
            "page_size": p["page_size"],
            "results": filas[inicio:inicio + p["page_size"]],
        })


class NecesidadesParamsSerializer(FiltrosPlaneacionSerializer):
    variante = serializers.IntegerField(min_value=1, required=False)
    cantidad = serializers.IntegerField(min_value=1, max_value=1_000_000, required=False)

    def validate(self, attrs):
        if "cantidad" in attrs and "variante" not in attrs:
            raise serializers.ValidationError({"cantidad": "Solo aplica con variante."})
        return attrs


class NecesidadesMateriaPrimaView(APIView):
    """``GET /api/v1/planeacion/necesidades-materia-prima/``.

    - Con ``variante`` (y ``cantidad``, default 1): desglose del BOM de ese SKU, lo
      que alcanza cada insumo y lo que falta para fabricar ``cantidad``.
    - Sin ``variante``: materia prima consolidada para fabricar el
      ``resurtido_sugerido`` de las filas del tablero con los mismos filtros
      (``agrupar`` se ignora: siempre por variante).
    """

    permission_classes = [IsAuthenticated, RequiereCodigo]
    codigos_requeridos = CODIGOS_PLANEACION

    def get(self, request):
        empresa, p, error = _params(request, NecesidadesParamsSerializer)
        if empresa is None:
            return error
        if p.get("variante"):
            return self._una_variante(empresa, p["variante"], p.get("cantidad", 1), _ambito(request.user, p))

        p = {**p, "agrupar": "variante", "ordering": "-vendido_periodo"}
        resultado = tablero.calcular(empresa, p, _ambito(request.user, p))
        filas = [r for r in resultado["filas"] if r["resurtido_sugerido"] > 0]
        cantidades = {r["variante_id"]: r["resurtido_sugerido"] for r in filas}
        componentes, sin_bom = capacidad.necesidades(resultado["boms"], cantidades)
        info = {r["variante_id"]: r for r in filas}
        return Response({
            "parametros": resultado["parametros"],
            "resumen": {
                "variantes_a_resurtir": len(filas),
                "piezas_a_resurtir": sum(cantidades.values()),
                "variantes_sin_bom": len(sin_bom),
                "componentes": len(componentes),
                "componentes_con_faltante": sum(1 for c in componentes if not c["alcanza"]),
                "componentes_unidad_inconsistente": sum(1 for c in componentes if c["unidad_inconsistente"]),
            },
            "componentes": componentes,
            "variantes_sin_bom": [
                {"variante_id": v, "sku": info[v]["sku"], "producto_nombre": info[v]["producto_nombre"],
                 "resurtido_sugerido": info[v]["resurtido_sugerido"]}
                for v in sin_bom
            ],
        })

    @staticmethod
    def _una_variante(empresa, variante_id, cantidad, ambito):
        v = (
            ProductoVariante.objects.filter(pk=variante_id, empresa=empresa)
            .values("id", "sku", "producto_id", "producto__nombre").first()
        )
        if v is None:
            return Response({"detail": "No encontrado."}, status=404)
        bom = capacidad.materiales(empresa, [variante_id], ambito).get(variante_id)
        base = {"variante_id": v["id"], "sku": v["sku"], "producto_id": v["producto_id"],
                "producto_nombre": v["producto__nombre"], "cantidad": cantidad}
        if bom is None:
            return Response({**base, **capacidad.resumen_variante(None), "bom_id": None,
                             "puede_fabricar": None, "materiales": []})
        materiales = []
        for m in bom["materiales"]:
            requerido = (m["por_pieza"] * cantidad).quantize(capacidad.CUATRO_DECIMALES)
            materiales.append({**m, "requerido": requerido,
                               "faltante": max(capacidad.CERO, requerido - m["existencia"])})
        resumen = capacidad.resumen_variante(bom)
        return Response({
            **base, **resumen, "bom_id": bom["bom_id"],
            "puede_fabricar": (
                resumen["fabricable_con_existencia"] >= cantidad
                if resumen["fabricable_con_existencia"] is not None else None
            ),
            "materiales": materiales,
        })
