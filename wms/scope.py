"""Alcance multi-tenant de los documentos de WMS con detalle propio.

Mismo criterio que ``produccion/scope.py`` (EC-880): SIEMPRE la empresa activa
del usuario (``user.empresa``), superusuario incluido. Antes de EC-882 el
superusuario veía pickings, packings y transferencias de todas las empresas, y
el PDF de un documento ajeno salía con el encabezado de su empresa activa.

Lo usan ``TransferenciaViewSet``, ``PickingViewSet`` y ``PackingViewSet``
(list, retrieve y, vía ``get_object``, ``trasladar``). Despacho y etiquetas RFID
conservan su propio criterio y no pasan por aquí.

No aplica ``select_related``/``prefetch_related`` ni orden: eso sigue siendo de
cada ViewSet.
"""


def documentos_wms_visibles(qs, user):
    """Acota ``qs`` (modelo con ``empresa`` y ``sucursal``) a lo que ``user`` puede leer.

    Sin empresa no se ve nada (cualquier usuario, incluido el superusuario).
    Dentro de la empresa, superusuario e ``is_admin_empresa`` ven todas las
    sucursales; el resto sólo las de ``user.sucursales_permitidas()`` (M2M
    ``sucursales`` + ``sucursal_default``). Se filtra por la sucursal dueña del
    documento, no por la de sus almacenes, que pueden diferir.

    list devuelve ``200 []`` fuera de alcance; retrieve de otra empresa o
    sucursal, ``404`` (no 403).
    """
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return qs
    return qs.filter(sucursal_id__in=user.sucursales_permitidas())
