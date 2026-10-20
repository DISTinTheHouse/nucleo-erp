"""Payload de ``POST /3/cfdis`` (CFDI 4.0 de ingreso, API Web) a partir de una ``Factura``.

El emisor no viaja en el payload: es el perfil fiscal de la cuenta única de
Facturama. Doc: https://facturama.mx/docs/api-reference/endpoint/factura-cfdi/crea-un-cfdi-de-emision

Fuentes, en orden de preferencia:
- Receptor: los datos fiscales que el pedido congeló al capturarse
  (``cliente_rfc``, ``cliente_razon_social``...) y, si vienen vacíos, el ``Cliente``.
- Forma/método de pago y uso de CFDI: el pedido (el uso cae al del cliente).
- Conceptos: los renglones de la factura con las claves SAT del producto.

Antes de llamar a Facturama se junta TODO lo que falta y se regresa en un solo
``ErrorDeNegocio``: corregir un dato y volver a intentar no debe descubrir el
siguiente faltante de uno en uno.
"""

import re
from decimal import Decimal

from finanzas.exceptions import ErrorDeNegocio

RFC_RE = re.compile(r"^[A-Z&Ñ]{3,4}[0-9]{2}(0[1-9]|1[012])(0[1-9]|[12][0-9]|3[01])[A-Z0-9]{2}[0-9A]$")
CP_RE = re.compile(r"^[0-9]{5}$")

METODOS_PAGO = ("PUE", "PPD")
# Regla del SAT: un CFDI PPD lleva FormaPago 99 (por definir); la forma real va en el REP.
FORMA_PAGO_POR_DEFINIR = "99"


def _texto(valor):
    return (valor or "").strip()


def _numero(valor):
    # ``requests`` serializa con ``json.dumps``, que no acepta ``Decimal``.
    return float(valor or Decimal("0"))


def _receptor(factura, errores):
    pedido, cliente = factura.pedido, factura.cliente
    rfc = _texto(getattr(pedido, "cliente_rfc", None)) or _texto(cliente.rfc)
    nombre = _texto(getattr(pedido, "cliente_razon_social", None)) or _texto(cliente.razon_social)
    regimen = getattr(pedido, "cliente_regimen_fiscal", None) or cliente.sat_regimen_fiscal
    cp = _texto(getattr(pedido, "cliente_codigo_postal", None)) or _texto(cliente.codigo_postal)
    uso = _texto(getattr(pedido, "uso_cfdi", None)) or _texto(getattr(cliente.sat_uso_cfdi, "codigo", None))

    rfc = rfc.upper()
    if not RFC_RE.match(rfc):
        errores["rfc"] = f"El RFC del cliente ({rfc or 'vacío'}) no es válido."
    if not nombre:
        errores["razon_social"] = "El cliente no tiene razón social (debe ser igual a su constancia fiscal)."
    if regimen is None:
        errores["regimen_fiscal"] = "El cliente no tiene régimen fiscal."
    if not CP_RE.match(cp):
        errores["codigo_postal"] = "El cliente no tiene código postal fiscal válido (5 dígitos)."
    if not uso:
        errores["uso_cfdi"] = "Ni el pedido ni el cliente tienen uso de CFDI."

    return {
        "Rfc": rfc,
        "Name": nombre,
        "CfdiUse": uso,
        "FiscalRegime": getattr(regimen, "codigo", ""),
        "TaxZipCode": cp,
    }


def _conceptos(factura, errores):
    renglones = list(
        factura.factura_detalles.select_related(
            "producto__sat_prodserv", "producto__sat_unidad", "pedido_detalle_talla__talla"
        ).order_by("id")
    )
    if not renglones:
        errores["factura_detalles"] = "La factura no tiene conceptos."
        return []

    sin_claves = sorted({
        r.producto.nombre for r in renglones
        if r.producto.sat_prodserv is None or r.producto.sat_unidad is None
    })
    if sin_claves:
        errores["productos"] = (
            "Productos sin clave SAT de producto/servicio o de unidad: " + ", ".join(sin_claves) + "."
        )
        return []

    conceptos = []
    for r in renglones:
        producto = r.producto
        talla = getattr(getattr(r.pedido_detalle_talla, "talla", None), "nombre", None)
        base = r.subtotal - r.descuento
        concepto = {
            "ProductCode": producto.sat_prodserv.codigo,
            "Description": f"{producto.nombre} - Talla {talla}" if talla else producto.nombre,
            "Unit": producto.sat_unidad.descripcion[:20],
            "UnitCode": producto.sat_unidad.codigo,
            "UnitPrice": _numero(r.precio_unitario),
            "Quantity": _numero(r.cantidad),
            "Subtotal": _numero(r.subtotal),
            "Discount": _numero(r.descuento),
            "TaxObject": "02",
            "Taxes": [{
                "Name": "IVA",
                "Rate": _numero(r.porcentaje_impuesto / Decimal("100")),
                "Base": _numero(base),
                "Total": _numero(r.impuesto),
                "IsRetention": False,
            }],
            "Total": _numero(r.total),
        }
        identificador = _texto(producto.codigo) or _texto(producto.cod_proscai)
        if identificador:
            concepto["IdentificationNumber"] = identificador
        conceptos.append(concepto)
    return conceptos


def construir_payload_cfdi(factura):
    """Payload de Facturama; levanta ``ErrorDeNegocio`` con todos los faltantes juntos."""
    errores = {}
    pedido = factura.pedido

    lugar = _texto(factura.sucursal.cp)
    if not CP_RE.match(lugar):
        errores["sucursal"] = (
            f"La sucursal {factura.sucursal.nombre} no tiene código postal válido "
            "(5 dígitos) para el lugar de expedición."
        )
    if _texto(factura.moneda.codigo_iso).upper() != "MXN":
        errores["moneda"] = "Por ahora solo se timbran facturas en MXN (la factura no guarda tipo de cambio)."
    if not _texto(factura.folio):
        errores["folio"] = "La factura no tiene folio."

    metodo = forma = ""
    if pedido is None:
        errores["pedido"] = "La factura no tiene pedido: de ahí salen la forma y el método de pago."
    else:
        metodo = _texto(pedido.metodo_pago).upper()
        forma = _texto(pedido.forma_pago)
        if metodo not in METODOS_PAGO:
            errores["metodo_pago"] = f"El pedido no tiene método de pago PUE o PPD ({metodo or 'vacío'})."
        elif metodo == "PPD":
            forma = FORMA_PAGO_POR_DEFINIR
        elif not forma:
            errores["forma_pago"] = "El pedido no tiene forma de pago."

    receptor = _receptor(factura, errores)
    conceptos = _conceptos(factura, errores)
    if errores:
        # Un solo mensaje: el frontend muestra el primero de la primera clave.
        raise ErrorDeNegocio({"cfdi": "No se puede timbrar. " + " ".join(errores.values())})

    payload = {
        "NameId": "1",
        "CfdiType": "I",
        "ExpeditionPlace": lugar,
        "Currency": "MXN",
        "Exportation": "01",
        "PaymentForm": forma,
        "PaymentMethod": metodo,
        "Folio": factura.folio,
        "Receiver": receptor,
        "Items": conceptos,
    }
    serie = _texto(getattr(factura.serie_folio, "serie", None))
    if serie:
        payload["Serie"] = serie
    if pedido is not None and _texto(pedido.folio):
        payload["OrderNumber"] = _texto(pedido.folio)[:100]
    return payload


# --- REP: complemento de pago (CFDI tipo P) de un cobro ---------------------

FORMA_PAGO_COBRO = {"Efectivo": "01", "Cheque": "02", "Transferencia": "03", "Tarjeta": "04"}
CENTAVO = Decimal("0.01")


def construir_payload_rep(cobro):
    """Payload del REP de un cobro aplicado a facturas PPD timbradas.

    Por factura: parcialidad = cobros previos + 1 y saldo anterior = total menos
    lo cobrado antes (por ``fecha_cobro``, ``id``). El IVA se desglosa con la tasa
    de los renglones de la factura.
    """
    from finanzas.models import CobroDetalle

    detalles = list(cobro.cobro_detalles.select_related(
        "cxc__factura__pedido__cliente_regimen_fiscal", "cxc__factura__cliente__sat_regimen_fiscal",
        "cxc__factura__sucursal", "cxc__factura__serie_folio",
    ))
    errores = {}
    if not detalles:
        errores["detalles"] = "El cobro no tiene facturas aplicadas."
    forma = FORMA_PAGO_COBRO.get(cobro.metodo_pago)
    if not forma:
        errores["forma_pago"] = f"Método de cobro sin forma de pago SAT ({cobro.metodo_pago})."

    documentos = []
    for d in detalles:
        f = d.cxc.factura
        if not f.cfdi_uuid:
            errores[f"f{f.pk}"] = f"La factura {f.folio} no está timbrada."
            continue
        if _texto(getattr(f.pedido, "metodo_pago", None)).upper() != "PPD":
            errores[f"f{f.pk}"] = f"La factura {f.folio} no es PPD: no lleva complemento de pago."
            continue
        previos = CobroDetalle.objects.filter(
            cxc=d.cxc, cobro__activo=True, cobro__estatus="Aplicado",
        ).exclude(cobro=cobro).filter(_cobros_previos(cobro))
        pagado_antes = sum((p.importe_aplicado for p in previos), Decimal("0"))
        tasa = (f.factura_detalles.values_list("porcentaje_impuesto", flat=True).first() or Decimal("0")) / Decimal("100")
        base = (d.importe_aplicado / (1 + tasa)).quantize(CENTAVO)
        documentos.append({
            "Uuid": f.cfdi_uuid,
            "Serie": _texto(getattr(f.serie_folio, "serie", None)) or None,
            "Folio": f.folio,
            "Currency": "MXN",
            "EquivalenceDocRel": 1,
            "PartialityNumber": len(previos) + 1,
            "PreviousBalanceAmount": _numero(f.total - pagado_antes),
            "AmountPaid": _numero(d.importe_aplicado),
            "TaxObject": "02",
            "Taxes": [{"Name": "IVA", "Rate": _numero(tasa), "Base": _numero(base),
                       "Total": _numero(d.importe_aplicado - base), "IsRetention": False}],
        })

    factura = detalles[0].cxc.factura if detalles else None
    receptor = _receptor(factura, errores) if factura else {}
    lugar = _texto(factura.sucursal.cp) if factura else ""
    if factura and not CP_RE.match(lugar):
        errores["sucursal"] = "La sucursal de la factura no tiene código postal válido."
    if errores:
        raise ErrorDeNegocio({"rep": "No se puede timbrar el REP. " + " ".join(errores.values())})

    receptor["CfdiUse"] = "CP01"
    pago = {
        "Date": f"{cobro.fecha_cobro.isoformat()}T12:00:00",
        "PaymentForm": forma,
        "Currency": "MXN",
        "Amount": _numero(sum((d.importe_aplicado for d in detalles), Decimal("0"))),
        "RelatedDocuments": documentos,
    }
    if _texto(cobro.referencia_operacion):
        pago["OperationNumber"] = _texto(cobro.referencia_operacion)[:100]
    return {
        "NameId": "14",
        "CfdiType": "P",
        "ExpeditionPlace": lugar,
        "Receiver": receptor,
        "Complemento": {"Payments": [pago]},
    }


def _cobros_previos(cobro):
    """Cobros anteriores a éste: antes por fecha, o mismo día con id menor."""
    from django.db.models import Q

    return Q(cobro__fecha_cobro__lt=cobro.fecha_cobro) | Q(
        cobro__fecha_cobro=cobro.fecha_cobro, cobro_id__lt=cobro.pk
    )
