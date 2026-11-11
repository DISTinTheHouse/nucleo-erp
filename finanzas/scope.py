"""Alcance por sucursal de lo comercial de finanzas (#753): facturas, notas de crédito, CxC y cobros.

Superusuario y admin de empresa ven toda la empresa; el resto, sólo las sucursales de
``Usuario.sucursales_permitidas()``. Pólizas, bancos, CxP, conciliaciones y reportes
siguen por empresa: la contabilidad es de la empresa, no de la sucursal.
"""

from django.db.models import Exists, OuterRef
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
    """Un cobro se ve si todas sus líneas son de facturas visibles (sin líneas, también)."""
    ids = sucursales_visibles(user)
    if ids is None:
        return qs
    from finanzas.models import CobroDetalle

    ajenas = CobroDetalle.objects.filter(cobro=OuterRef("pk")).exclude(cxc__factura__sucursal_id__in=ids)
    return qs.exclude(Exists(ajenas))


def exigir_factura_visible(user, factura):
    """Escrituras sobre una factura de otra sucursal → 400, igual que una inexistente."""
    ids = sucursales_visibles(user)
    if factura is not None and ids is not None and factura.sucursal_id not in ids:
        raise ValidationError({"factura": "Factura no encontrada."})


def resolver_sucursal_documento(user, empresa, sucursal, sucursales_lineas):
    """Sucursal de un cobro o pago al crearlo (#1063). Siempre queda una.

    - La enviada: debe ser de la empresa y, para no admins, de sus sucursales.
    - Si no viene: la de la primera línea (factura de la CxC o de la CxP).
    - Si no hay líneas: la ``sucursal_default`` del usuario, si es de la empresa.
    """
    from nucleo.models import Sucursal

    empresa_id = getattr(empresa, "pk", empresa)
    if sucursal is None:
        candidatos = [s for s in sucursales_lineas if s] + [getattr(user, "sucursal_default_id", None)]
        sucursal = next(
            (s for s in (Sucursal.objects.filter(pk=c, empresa_id=empresa_id).first() for c in candidatos if c) if s),
            None,
        )
        if sucursal is None:
            raise ValidationError({"sucursal": "Indica la sucursal."})
    if sucursal.empresa_id != empresa_id:
        raise ValidationError({"sucursal": "La sucursal no pertenece a la empresa."})
    ids = sucursales_visibles(user)
    if ids is not None and sucursal.pk not in ids:
        raise ValidationError({"sucursal": "No tienes acceso a esa sucursal."})
    return sucursal
