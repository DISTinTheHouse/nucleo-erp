from rest_framework.exceptions import ValidationError


def parse_pk(raw):
    """Normaliza un id de query param: ausente/no numérico -> ``None``.

    Mismo criterio para ``pedido``/``almacen_origen``/``almacen_destino``:
    un valor no parseable se trata como ausente en vez de llegar crudo a un
    ``filter(pk=...)``, donde Django lo deja pasar sin validar y el driver de
    BD revienta con un ``ValueError`` no controlado (500) en lugar de un 400.
    """
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validar_contexto_picking(pedido, almacen, almacen_destino, operador, user):
    """Scope empresa/sucursal + permisos para encabezados de un nuevo picking.

    Esta validación es la puerta de entrada antes de cualquier escritura:
    valida consistencia de FKs y acceso del usuario sin abrir transacción.
    """
    empresa = getattr(user, "empresa", None)

    if empresa is None:
        raise ValidationError({"user": "El usuario no tiene una empresa asignada."})
    if pedido.empresa_id != empresa.pk:
        raise ValidationError({"pedido": "El pedido no pertenece a la empresa del usuario."})
    if almacen.empresa_id and almacen.empresa_id != pedido.empresa_id:
        raise ValidationError({"almacen": "El almacén origen no pertenece a la empresa del pedido."})
    if almacen.sucursal_id and almacen.sucursal_id != pedido.sucursal_id:
        raise ValidationError({"almacen": "El almacén origen no pertenece a la sucursal del pedido."})
    if not bool(getattr(almacen, "permite_salida", False)):
        raise ValidationError({
            "almacen": "Este almacén no permite salidas (configura `permite_salida=True` en el catálogo)."
        })
    if almacen_destino is None:
        raise ValidationError({"almacen_destino": "Selecciona un almacén destino para el picking."})
    if almacen_destino.empresa_id and almacen_destino.empresa_id != pedido.empresa_id:
        raise ValidationError({"almacen_destino": "El almacén destino no pertenece a la empresa del pedido."})
    if almacen_destino.sucursal_id and almacen_destino.sucursal_id != pedido.sucursal_id:
        raise ValidationError({"almacen_destino": "El almacén destino no pertenece a la sucursal del pedido."})
    if almacen.pk == almacen_destino.pk:
        raise ValidationError({
            "almacen_destino": "El almacén destino debe ser distinto al origen.",
            "almacen": "El almacén origen debe ser distinto al destino.",
        })
    if not bool(getattr(almacen_destino, "permite_entrada", False)):
        raise ValidationError({
            "almacen_destino": "Este almacén no permite entradas (configura `permite_entrada=True` en el catálogo)."
        })
    if getattr(operador, "empresa_id", None) != pedido.empresa_id:
        raise ValidationError({"operador": "El operador no pertenece a la empresa del pedido."})
    if not getattr(operador, "is_active", False):
        raise ValidationError({"operador": "El operador no está activo."})

    es_staff = getattr(user, "is_superuser", False) or getattr(
        user, "is_admin_empresa", False
    )
    if not es_staff:
        sucursales_permitidas = user.sucursales_permitidas()
        if pedido.sucursal_id not in sucursales_permitidas:
            raise ValidationError({
                "pedido": "No tiene acceso a la sucursal del pedido para generar el picking."
            })
        if almacen.sucursal_id and almacen.sucursal_id not in sucursales_permitidas:
            raise ValidationError({
                "almacen": "No tiene acceso a la sucursal del almacén origen seleccionado."
            })
        if (
            almacen_destino
            and almacen_destino.sucursal_id
            and almacen_destino.sucursal_id not in sucursales_permitidas
        ):
            raise ValidationError({
                "almacen_destino": "No tiene acceso a la sucursal del almacén destino seleccionado."
            })
