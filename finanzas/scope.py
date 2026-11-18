"""Alcance por sucursal de lo comercial de finanzas (#753): facturas, notas de crédito, CxC y cobros.

Superusuario y admin de empresa ven toda la empresa; el resto, sólo las sucursales de
``Usuario.sucursales_permitidas()``. Pólizas, bancos, CxP, conciliaciones y reportes
siguen por empresa: la contabilidad es de la empresa, no de la sucursal.
"""

from django.db.models import Exists, OuterRef, Q
from rest_framework.exceptions import ValidationError

from finanzas.exceptions import ErrorDeNegocio


def sucursales_visibles(user):
    """Ids de sucursal que ve ``user``; ``None`` = sin acotar."""
    if not getattr(user, "is_authenticated", False):
        return []  # p. ej. al generar el schema OpenAPI (#890)
    if user.is_superuser or getattr(user, "is_admin_empresa", False):
        return None
    return user.sucursales_permitidas()


def por_sucursal(qs, user, campo):
    """Filtra ``qs`` por ``campo``, la ruta a la sucursal (``"sucursal"``, ``"factura__sucursal"``, ``"pk"``)."""
    ids = sucursales_visibles(user)
    return qs if ids is None else qs.filter(**{f"{campo}__in": ids})


def cobros_por_sucursal(qs, user):
    """Un cobro se ve si su ``sucursal`` es visible y todas sus líneas son de facturas
    visibles (#1078).

    EC-874: un Borrador sin líneas no tiene sucursal (``null``): lo ve toda la empresa.
    Uno con líneas y sin sucursal (no debería existir) no.
    """
    ids = sucursales_visibles(user)
    if ids is None:
        return qs
    from finanzas.models import CobroDetalle

    lineas = CobroDetalle.objects.filter(cobro=OuterRef("pk"))
    ajenas = lineas.exclude(cxc__factura__sucursal_id__in=ids)
    return qs.filter(
        Q(sucursal_id__in=ids) | (Q(sucursal__isnull=True) & ~Exists(lineas))
    ).exclude(Exists(ajenas))


def exigir_factura_visible(user, factura):
    """Escrituras sobre una factura de otra sucursal → 400, igual que una inexistente."""
    ids = sucursales_visibles(user)
    if factura is not None and ids is not None and factura.sucursal_id not in ids:
        raise ValidationError({"factura": "Factura no encontrada."})


def exigir_sucursal_unica(sucursal_id, sucursales_lineas, *, documento, requerida=False):
    """La regla de EC-874 en un solo lugar: un cobro o pago es de una sola sucursal, la
    de las facturas de sus líneas. La usan las vistas (al crear y al editar líneas) y
    ``aplicar_cobro``/``aplicar_pago``.

    - Facturas de varias sucursales → 400.
    - Con ``sucursal_id`` (la del documento o la enviada): debe ser la de las facturas.
    - Con ``requerida`` (al aplicar): el documento debe tener sucursal.

    Devuelve el id de la sucursal de las líneas (``None`` sin líneas). Lanza
    ``ErrorDeNegocio``: los servicios no dependen de DRF y las vistas lo traducen a 400.
    """
    ids = {s for s in sucursales_lineas if s}
    if len(ids) > 1:
        raise ErrorDeNegocio({"sucursal": f"Las facturas son de varias sucursales: haz un {documento} por sucursal."})
    if requerida and sucursal_id is None:
        raise ErrorDeNegocio({"sucursal": f"El {documento} no tiene sucursal: vuelve a capturar sus líneas."})
    if sucursal_id is not None and ids and ids != {sucursal_id}:
        raise ErrorDeNegocio({"sucursal": f"La sucursal del {documento} no coincide con la de sus facturas."})
    return next(iter(ids), None)


def sucursales_de_cxcs(cxc_ids):
    """Sucursales (distintas) de las facturas de esas CxC, en una consulta."""
    from nucleo.models import Sucursal

    return list(Sucursal.objects.filter(facturas__cuentas_por_cobrar__in=cxc_ids).distinct()) if cxc_ids else []


def sucursales_de_cxps(cxp_ids):
    """Sucursales (distintas) de las facturas de proveedor de esas CxP, en una consulta."""
    from nucleo.models import Sucursal

    return list(Sucursal.objects.filter(facturas_proveedores__cuentas_por_pagar__in=cxp_ids).distinct()) if cxp_ids else []


def resolver_sucursal_documento(empresa, enviada, sucursales_lineas, *, documento, user=None):
    """Sucursal de un cobro o pago (EC-874): la de las facturas de sus líneas.

    ``sucursales_lineas`` son las ``Sucursal`` de esas facturas, sin repetir
    (``sucursales_de_cxcs``/``sucursales_de_cxps``: una consulta, sin otra para validar).

    - Sin líneas (borrador): ``None``. La ``enviada`` se ignora: la sucursal sale de
      las facturas cuando lleguen.
    - Varias sucursales o una ``enviada`` distinta → 400 (``exigir_sucursal_unica``).
    - La de las facturas debe ser de la empresa → si no, 400. Puede estar inactiva: el
      cobro o pago salda una deuda que ya existe.
    - Con ``user`` (cobros), además debe ser visible para él; los pagos van por
      empresa, como las CxP (``user=None``). Es defensa en profundidad: a través de la
      API la visibilidad la impone el queryset de ``cxc`` del serializer (una CxC de otra
      sucursal responde como un id inexistente), así que el mensaje de aquí no es parte
      del contrato.
    """
    sucursales_lineas = list(sucursales_lineas)
    if not sucursales_lineas:
        return None
    exigir_sucursal_unica(
        getattr(enviada, "pk", None), [s.pk for s in sucursales_lineas], documento=documento,
    )
    de_lineas = sucursales_lineas[0]
    if de_lineas.empresa_id != getattr(empresa, "pk", empresa):
        raise ValidationError({"sucursal": "La sucursal de las facturas no pertenece a la empresa."})
    if user is not None:
        visibles = sucursales_visibles(user)
        if visibles is not None and de_lineas.pk not in visibles:
            raise ValidationError({"sucursal": "No tienes acceso a esa sucursal."})
    return de_lineas
