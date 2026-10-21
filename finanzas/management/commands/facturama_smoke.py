"""Prueba de humo contra el SANDBOX de Facturama, sin tocar la BD.

    python manage.py facturama_smoke

Timbra una factura PPD, su REP y una nota de crédito con la misma forma de payload
que arma ``finanzas/services/facturama/cfdi_payload.py``, y luego los cancela
(relacionados primero). Se niega a correr si ``FACTURAMA_BASE_URL`` no es sandbox.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from finanzas.services.factura_timbrado_service import _mensaje, _sello
from finanzas.services.facturama.exceptions import FacturamaAPIException
from finanzas.services.facturama.service import FacturamaService

# Receptor de prueba del SAT (ejemplo de la doc de Facturama).
RECEPTOR = {"Rfc": "URE180429TM6", "Name": "UNIVERSIDAD ROBOTICA ESPAÑOLA", "FiscalRegime": "601", "TaxZipCode": "86991"}


def _iva(base, tasa=0.16):
    return [{"Name": "IVA", "Rate": tasa, "Base": base, "Total": round(base * tasa, 2), "IsRetention": False}]


def factura(lugar):
    return {
        "NameId": "1", "CfdiType": "I", "ExpeditionPlace": lugar, "Currency": "MXN", "Exportation": "01",
        "PaymentForm": "99", "PaymentMethod": "PPD", "Folio": "SMOKE-1",
        "Receiver": {**RECEPTOR, "CfdiUse": "G03"},
        "Items": [{
            "ProductCode": "53102700", "Description": "Playera - Talla M", "Unit": "Pieza", "UnitCode": "H87",
            "UnitPrice": 100.0, "Quantity": 2.0, "Subtotal": 200.0, "Discount": 0.0, "TaxObject": "02",
            "Taxes": _iva(200.0), "Total": 232.0,
        }],
    }


def rep(lugar, uuid):
    return {
        "NameId": "14", "CfdiType": "P", "ExpeditionPlace": lugar, "Receiver": {**RECEPTOR, "CfdiUse": "CP01"},
        "Complemento": {"Payments": [{
            "Date": f"{timezone.localdate().isoformat()}T12:00:00", "PaymentForm": "03", "Currency": "MXN", "Amount": 116.0,
            "RelatedDocuments": [{
                "Uuid": uuid, "Folio": "SMOKE-1", "Currency": "MXN", "EquivalenceDocRel": 1, "PartialityNumber": 1,
                "PreviousBalanceAmount": 232.0, "AmountPaid": 116.0, "TaxObject": "02", "Taxes": _iva(100.0),
            }],
        }]},
    }


def nota_credito(lugar, uuid):
    return {
        "NameId": "2", "CfdiType": "E", "ExpeditionPlace": lugar, "Currency": "MXN", "Exportation": "01",
        "PaymentForm": "03", "PaymentMethod": "PUE", "Folio": "SMOKE-NC1",
        "Receiver": {**RECEPTOR, "CfdiUse": "G02"},
        "Relations": {"Type": "01", "Cfdis": [{"Uuid": uuid}]},
        "Items": [{
            "ProductCode": "84111506", "Description": "Nota de crédito SMOKE-NC1: Descuento", "Unit": "Actividad",
            "UnitCode": "ACT", "UnitPrice": 50.0, "Quantity": 1.0, "Subtotal": 50.0, "TaxObject": "02",
            "Taxes": _iva(50.0), "Total": 58.0,
        }],
    }


class Command(BaseCommand):
    help = "Timbra y cancela en el sandbox de Facturama una factura, su REP y una nota de crédito."

    def handle(self, *args, **options):
        if "apisandbox" not in (settings.FACTURAMA_BASE_URL or ""):
            raise CommandError("Solo corre contra el sandbox (FACTURAMA_BASE_URL con apisandbox).")
        self.servicio = FacturamaService()
        # El lugar de expedición debe ser una sucursal dada de alta en el perfil fiscal.
        sucursales = self.servicio.client.get("/BranchOffice") or []
        lugar = ((sucursales[0] if sucursales else {}).get("Address") or {}).get("ZipCode")
        if not lugar:
            raise CommandError("La cuenta de Facturama no tiene lugares de expedición.")
        self.stdout.write(f"Lugar de expedición: {lugar}")
        timbrados = []
        f = self._timbrar("Factura PPD", factura(lugar))
        if f:
            timbrados.append(("Factura PPD", f))
            for nombre, payload in (("REP", rep(lugar, f[0])), ("Nota de crédito", nota_credito(lugar, f[0]))):
                doc = self._timbrar(nombre, payload)
                if doc:
                    timbrados.insert(0, (nombre, doc))
        for nombre, (_, facturama_id) in timbrados:  # relacionados primero, factura al final
            self._cancelar(nombre, facturama_id)

    def _timbrar(self, nombre, payload):
        try:
            uuid, facturama_id, _ = _sello(self.servicio.create_cfdi_emision(payload))
        except FacturamaAPIException as exc:
            self.stdout.write(self.style.ERROR(f"✗ {nombre}: {_mensaje(exc)}"))
            return None
        self.stdout.write(self.style.SUCCESS(f"✓ {nombre}: UUID {uuid} (Id {facturama_id})"))
        return uuid, facturama_id

    def _cancelar(self, nombre, facturama_id):
        try:
            estatus = (self.servicio.cancel_cfdi(facturama_id) or {}).get("Status")
            self.stdout.write(self.style.SUCCESS(f"✓ Cancelar {nombre}: {estatus}"))
        except FacturamaAPIException as exc:
            self.stdout.write(self.style.ERROR(f"✗ Cancelar {nombre}: {_mensaje(exc)}"))
