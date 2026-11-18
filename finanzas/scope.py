"""Alcance por sucursal de lo comercial de finanzas (#753): facturas, notas de crédito, CxC y cobros.

Superusuario y admin de empresa ven toda la empresa; el resto, sólo las sucursales de
``Usuario.sucursales_permitidas()``. Pólizas, bancos, CxP, conciliaciones y reportes
siguen por empresa: la contabilidad es de la empresa, no de la sucursal.
"""

from django.db.models import Exists, OuterRef, Q
from rest_framework.exceptions import ValidationError


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


def resolver_sucursal_documento(empresa, enviada, sucursales_lineas, *, documento, user=None):
    """Sucursal de un cobro o pago (EC-874): la de las facturas de sus líneas.

    Un cobro o pago es de una sola sucursal; de ella salen su folio y su póliza.

    - Sin líneas (borrador): ``None``. La ``enviada`` se ignora: la sucursal sale de
      las facturas cuando lleguen.
    - Facturas de varias sucursales → 400.
    - La de las facturas debe ser de la empresa → si no, 400. Puede estar inactiva: el
      cobro o pago salda una deuda que ya existe (la ``enviada`` sí debe estar activa,
      eso lo valida el queryset del serializer).
    - Una ``enviada`` distinta de la de las facturas → 400.
    - Con ``user`` (cobros), además debe ser visible para él. Los pagos van por
      empresa, como las CxP (``user=None``).

    ``sucursales_lineas`` son los ids de sucursal de las facturas de las líneas.
    """
    from nucleo.models import Sucursal

    ids = {s for s in sucursales_lineas if s}
    if not ids:
        return None
    if len(ids) > 1:
        raise ValidationError({"sucursal": f"Las facturas son de varias sucursales: haz un {documento} por sucursal."})
    empresa_id = getattr(empresa, "pk", empresa)
    de_lineas = Sucursal.objects.filter(pk=ids.pop(), empresa_id=empresa_id).first()
    if de_lineas is None:
        raise ValidationError({"sucursal": "La sucursal de las facturas no pertenece a la empresa."})
    if enviada is not None and enviada.pk != de_lineas.pk:
        raise ValidationError({"sucursal": f"No coincide con la de las facturas ({de_lineas.nombre})."})
    if user is not None:
        visibles = sucursales_visibles(user)
        if visibles is not None and de_lineas.pk not in visibles:
            raise ValidationError({"sucursal": "No tienes acceso a esa sucursal."})
    return de_lineas
