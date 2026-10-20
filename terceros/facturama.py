"""Clientes del ERP en el catálogo de clientes de Facturama (EC-510/511/512).

Solo para la empresa dueña de la cuenta. Si Facturama no guarda el cambio se
levanta ``ValidationError`` y quien llama (dentro de un ``atomic``) no lo guarda
en el ERP tampoco.
"""

from rest_framework.exceptions import ValidationError

from finanzas.services.factura_timbrado_service import _mensaje
from finanzas.services.facturama.acceso import empresa_usa_facturama
from finanzas.services.facturama.exceptions import FacturamaAPIException
from finanzas.services.facturama.service import FacturamaService


def _payload(cliente):
    datos = {
        "Email": (cliente.correo or "").strip(),
        "Rfc": (cliente.rfc or "").strip().upper(),
        "Name": (cliente.razon_social or cliente.nombre or "").strip(),
        "FiscalRegime": getattr(cliente.sat_regimen_fiscal, "codigo", ""),
        "CfdiUse": getattr(cliente.sat_uso_cfdi, "codigo", ""),
        "TaxZipCode": (cliente.codigo_postal or "").strip(),
    }
    domicilio = {
        "Street": (cliente.direccion_fiscal or "").strip(),
        "Neighborhood": (cliente.colonia or "").strip(),
        "ZipCode": (cliente.codigo_postal or "").strip(),
        "Municipality": (cliente.ciudad or "").strip(),
        "State": (cliente.estado or "").strip(),
        "Country": "MEXICO",
    }
    datos["Address"] = {k: v for k, v in domicilio.items() if v}
    return {k: v for k, v in datos.items() if v}


def _llamar(metodo, ruta, **kwargs):
    try:
        return getattr(FacturamaService().client, metodo)(ruta, **kwargs)
    except FacturamaAPIException as exc:
        raise ValidationError({"facturama": f"Facturama no guardó el cambio: {_mensaje(exc)}"})


def sincronizar(cliente):
    """Alta (guarda el Id) o actualización en Facturama. ``False`` si la empresa no usa Facturama."""
    if not empresa_usa_facturama(cliente.empresa):
        return False
    datos = _payload(cliente)
    if cliente.facturama_id:
        _llamar("put", f"/Client/{cliente.facturama_id}", json={**datos, "Id": cliente.facturama_id})
    else:
        respuesta = _llamar("post", "/Client", json=datos) or {}
        cliente.facturama_id = respuesta.get("Id")
        cliente.save(update_fields=["facturama_id"])
    return True


def eliminar(cliente):
    if cliente.facturama_id and empresa_usa_facturama(cliente.empresa):
        _llamar("delete", f"/Client/{cliente.facturama_id}")


def consultar(cliente):
    if not cliente.facturama_id:
        raise ValidationError({"facturama": "El cliente no está en Facturama."})
    return _llamar("get", f"/Client/{cliente.facturama_id}")
