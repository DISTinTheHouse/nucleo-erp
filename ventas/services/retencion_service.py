"""Retención de pedidos por mesa de control.

Un pedido retenido (crédito, cliente pidió esperar, cambio por confirmar) no se
cancela: se congela. Mientras esté retenido nadie inicia trabajo nuevo sobre él:

- almacén: no se surte (picking), no se empaca (packing) ni se despacha;
- producción: no se crean OP, OB, OR ni OCM ligadas al pedido.

Lo que ya está en manos de los operadores (avances de órdenes abiertas, pickings
ya creados) no se detiene aquí: se cancela con su propio endpoint si hace falta.
"""

from rest_framework.exceptions import ValidationError


def mensaje_retenido(pedido):
    motivo = f": {pedido.motivo_retencion}" if pedido.motivo_retencion else ""
    return (
        f"El pedido {pedido.folio or pedido.pk} está retenido por mesa de control{motivo}. "
        "Pide a mesa de control que lo libere."
    )


def retenido_por_nombre(pedido):
    """Quién retuvo el pedido (EC-953): nombre completo o, sin first/last name, su
    correo (misma regla que ``cancelado_por_nombre``, EC-926/EC-949); ``None`` si no
    está retenido. Sin consultas cuando ``retenido_por`` ya viene cargado."""
    if not pedido.retenido or not pedido.retenido_por_id:
        return None
    usuario = pedido.retenido_por
    return usuario.get_full_name().strip() or usuario.email


def registrar_bitacora(pedido, user, retenido):
    """Retener/liberar no cambia ``estatus``, así que la señal de la bitácora no lo ve:
    se registra aquí como cambio de estatus legible para que salga en
    ``GET /api/v1/auditoria/documentos/?tipo=pedido`` y en ``linea-tiempo/``.
    El motivo completo queda en el ``AuditoriaEvento`` y en el pedido."""
    from auditoria.models import BitacoraDocumento

    BitacoraDocumento.objects.create(
        empresa_id=pedido.empresa_id, sucursal_id=pedido.sucursal_id, tipo_documento="pedido",
        documento_id=str(pedido.pk), folio=(pedido.folio or "")[:60],
        accion=BitacoraDocumento.Accion.ESTATUS,
        estatus_anterior="Activo" if retenido else "Retenido",
        estatus_nuevo="Retenido" if retenido else "Activo",
        estatus_codigo="RETENIDO" if retenido else "LIBERADO",
        total=pedido.gran_total, moneda_id=pedido.moneda_id, usuario=user,
    )


def exigir_no_retenido(pedido, campo="pedido"):
    """Sin consultas: ``retenido`` viene en la fila del pedido ya cargada."""
    if pedido is not None and pedido.retenido:
        raise ValidationError({campo: mensaje_retenido(pedido)})


def bloquear_pedido_vivo(pedido_id):
    """Alta de trabajo nuevo sobre el pedido (packing, despacho, OB, OR, OCM): bloquea
    su fila (primer paso del orden EC-574) y revisa sobre ella lo que antes se leía
    sin candado, para que un ``cancelar/`` o ``retener/`` simultáneo no se cuele.
    Inexistente -> 400 (antes seguía y reventaba con ``IntegrityError``, #1368);
    cancelado o dado de baja -> 400; retenido -> 400. Picking repite estas reglas
    con sus propios mensajes (EC-953). Una consulta."""
    from ventas.models import Pedido

    pedido = Pedido.objects.select_for_update(of=("self",)).filter(pk=pedido_id).first()
    if pedido is None:
        raise ValidationError({"pedido": "Pedido no encontrado o sin acceso."})
    if pedido.estatus == Pedido.ESTATUS_CANCELADO or not pedido.activo:
        raise ValidationError({"pedido": f"El pedido {pedido.folio or pedido.pk} está cancelado o dado de baja."})
    exigir_no_retenido(pedido)
    return pedido
