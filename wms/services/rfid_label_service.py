import secrets
import time
from decimal import Decimal

from django.db import transaction
from django.template.defaultfilters import truncatechars
from rest_framework.exceptions import ValidationError

from catalogo.models import Producto, ProductoVariante
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion


class RFIDLabelService:
    @staticmethod
    def _allowed_sucursal_ids(user):
        sucursal_ids = set(user.sucursales.values_list("pk", flat=True))
        if getattr(user, "sucursal_default_id", None):
            sucursal_ids.add(user.sucursal_default_id)
        return sucursal_ids

    @staticmethod
    def _build_label_preview(variante=None, producto=None):
        if variante is not None:
            producto_base = variante.producto
            color = getattr(variante.color, "nombre", "") or ""
            talla = getattr(variante.talla, "nombre", "") or ""
            secondary = " / ".join(
                value for value in [color.upper(), talla.upper()] if value
            )
            meta_raw = producto_base.codigo or producto_base.cod_proscai
            return {
                "header": (
                    f"SKU {variante.sku} · {producto_base.nombre}"
                    if variante.sku
                    else producto_base.nombre
                ),
                "title": producto_base.nombre,
                "primary_line": f"SKU: {variante.sku}",
                "secondary_line": secondary,
                "meta_line": f"COD: {meta_raw}" if meta_raw else "",
                "barcode_value": variante.sku or str(variante.pk),
            }

        if producto is not None:
            codigo_impresion = producto.codigo or producto.cod_proscai or str(producto.pk)
            meta_line = ""
            if producto.cod_proscai and producto.cod_proscai != producto.codigo:
                meta_line = f"PROSCAI: {producto.cod_proscai}"
            return {
                "header": f"COD {codigo_impresion} · {producto.nombre}",
                "title": producto.nombre,
                "primary_line": f"COD: {codigo_impresion}",
                "secondary_line": "",
                "meta_line": meta_line,
                "barcode_value": codigo_impresion,
            }

        return None

    @staticmethod
    def _generate_epc_base_prefix(variante=None, producto=None):
        if variante is not None:
            seed = f"V{variante.pk}-{variante.sku or 'X'}"
        elif producto is not None:
            seed = f"P{producto.pk}-{producto.codigo or producto.cod_proscai or 'X'}"
        else:
            seed = "G"
        hashed = sum(ord(ch) * (i + 1) for i, ch in enumerate(seed)) & 0xFFFFFFFF
        return f"{hashed:08X}"

    @classmethod
    def _generate_epc_list(cls, cantidad, variante=None, producto=None):
        prefix = cls._generate_epc_base_prefix(variante=variante, producto=producto)
        ts_chunk = f"{int(time.time()) & 0xFFFFFFFF:08X}"
        result = []
        for idx in range(1, max(1, int(cantidad)) + 1):
            serial = f"{idx:04X}{secrets.token_hex(2).upper()}"
            epc = f"{prefix}{ts_chunk}{serial}"
            result.append(
                {
                    "n": idx,
                    "epc": epc,
                    "serial": f"{idx:04d}",
                }
            )
        return result

    @staticmethod
    def _graphic_zpl_lines(variante=None, producto=None, barcode_value=""):
        lines = [
            "^XA",
            "^PW799",
            "^LL400",
            "^CI28",
            "^LH0,0",
            "^FO40,30^A0N,34,34^FDWMS - ETIQUETA RFID^FS",
        ]

        if variante is not None:
            prod = variante.producto
            nombre = truncatechars((prod.nombre or "").upper(), 32)
            sku = (variante.sku or "").upper()
            color = getattr(variante.color, "nombre", "") or ""
            talla = getattr(variante.talla, "nombre", "") or ""
            secondary = " / ".join(v.upper() for v in [color, talla] if v)
            codigo = (prod.codigo or prod.cod_proscai or "").upper()

            lines.append(f"^FO40,85^A0N,32,32^FD{nombre}^FS")
            lines.append(f"^FO40,130^A0N,28,28^FDSKU: {sku}^FS")
            if secondary:
                lines.append(f"^FO40,168^A0N,28,28^FD{secondary}^FS")
            if codigo:
                lines.append(f"^FO40,206^A0N,26,26^FDCOD: {codigo}^FS")
            lines.append(
                f"^FO40,245^BY3,3,90^BCN,90,Y,N,N^FD{barcode_value or sku}^FS"
            )
            return lines

        if producto is not None:
            nombre = truncatechars((producto.nombre or "").upper(), 32)
            codigo_impresion = (
                producto.codigo or producto.cod_proscai or f"PROD-{producto.pk}"
            ).upper()
            auxiliar = (producto.cod_proscai or "").upper()

            lines.append(f"^FO40,85^A0N,32,32^FD{nombre}^FS")
            lines.append(f"^FO40,130^A0N,28,28^FDCODIGO: {codigo_impresion}^FS")
            if auxiliar and auxiliar != codigo_impresion:
                lines.append(f"^FO40,168^A0N,26,26^FDPROSCAI: {auxiliar}^FS")
            lines.append(
                f"^FO40,245^BY3,3,90^BCN,90,Y,N,N^FD{barcode_value or codigo_impresion}^FS"
            )
            return lines

        return lines

    @classmethod
    def _build_zpl_normal(cls, variante=None, producto=None, barcode_value=""):
        lines = cls._graphic_zpl_lines(
            variante=variante, producto=producto, barcode_value=barcode_value
        )
        lines.append("^FO40,360^A0N,22,22^FDEtiqueta generada desde ERP.^FS")
        lines.append("^XZ")
        return "\n".join(lines)

    @classmethod
    def _build_zpl_rfid(cls, epc, variante=None, producto=None, barcode_value=""):
        lines = cls._graphic_zpl_lines(
            variante=variante, producto=producto, barcode_value=barcode_value
        )
        lines.append("^RFW,E,,N")
        lines.append(f"^FD{epc}^FS")
        lines.append(
            f"^FO40,360^A0N,18,18^FDEPC: {epc[:16]}...{epc[-8:]}^FS"
        )
        lines.append("^XZ")
        return "\n".join(lines)

    @classmethod
    def _resolve_context(cls, user, variante_id=None, producto_id=None):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")

        variante = None
        producto = None

        if variante_id:
            variante = (
                ProductoVariante.objects.select_related("producto", "color", "talla")
                .filter(pk=variante_id, empresa=empresa, activo=True)
                .first()
            )
            if variante is None:
                raise ValidationError(
                    "La variante no existe o no pertenece a la empresa del usuario."
                )
            producto = variante.producto
        elif producto_id:
            producto = Producto.objects.filter(
                pk=producto_id, empresa=empresa, activo=True
            ).first()
            if producto is None:
                raise ValidationError(
                    "El producto no existe o no pertenece a la empresa del usuario."
                )
        else:
            raise ValidationError(
                "Debe proporcionar 'variante' o 'producto' para generar la etiqueta."
            )

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal = None
        if getattr(user, "sucursal_default_id", None):
            sucursal_id = user.sucursal_default_id
            if es_staff or sucursal_id in cls._allowed_sucursal_ids(user):
                sucursal = sucursal_id

        return empresa, sucursal, producto, variante

    @classmethod
    def onboarding_preview(cls, user, variante_id=None, producto_id=None, cantidad=1, rfid_mode=True):
        empresa, sucursal, producto, variante = cls._resolve_context(
            user, variante_id=variante_id, producto_id=producto_id
        )
        preview = cls._build_label_preview(variante=variante, producto=producto)
        barcode_value = preview["barcode_value"]

        zpl_normal = cls._build_zpl_normal(
            variante=variante, producto=producto, barcode_value=barcode_value
        )

        etiquetas_metadata = cls._generate_epc_list(
            cantidad, variante=variante, producto=producto
        )
        for item in etiquetas_metadata:
            item["barcode_value"] = barcode_value

        zpl_rfid_first = ""
        if rfid_mode and etiquetas_metadata:
            zpl_rfid_first = cls._build_zpl_rfid(
                etiquetas_metadata[0]["epc"],
                variante=variante,
                producto=producto,
                barcode_value=barcode_value,
            )

        payload = {
            "empresa": empresa.pk,
            "sucursal": sucursal,
            "cantidad": max(1, int(cantidad)),
            "rfid_mode": bool(rfid_mode),
            "producto": (
                {
                    "id": producto.pk,
                    "nombre": producto.nombre,
                    "codigo": producto.codigo,
                    "cod_proscai": producto.cod_proscai,
                }
                if producto
                else None
            ),
            "producto_variante": (
                {
                    "id": variante.pk,
                    "sku": variante.sku,
                    "nombre": getattr(variante, "nombre", None),
                    "color": getattr(variante.color, "nombre", None),
                    "talla": getattr(variante.talla, "nombre", None),
                }
                if variante
                else None
            ),
            "preview_data": preview,
            "zpl_normal": zpl_normal,
            "zpl_rfid_first": zpl_rfid_first,
            "etiquetas": etiquetas_metadata,
        }
        return payload

    @staticmethod
    @transaction.atomic
    def store_impresion(data, user):
        servicio = RFIDLabelService
        empresa, sucursal, producto, variante = servicio._resolve_context(
            user,
            variante_id=data.get("producto_variante").pk
            if data.get("producto_variante")
            else None,
            producto_id=data.get("producto").pk
            if data.get("producto")
            else None,
        )

        cantidad = int(data.get("cantidad") or 1)
        rfid_mode = bool(data.get("rfid_mode", True))
        etiquetas_input = data.get("etiquetas") or []
        zpl_enviado = data.get("zpl_enviado") or None
        observaciones = data.get("observaciones") or None
        printer_name = data.get("printer_name") or None
        printer_address = data.get("printer_address") or None
        status_input = (data.get("status") or EtiquetaRFIDImpresion.Estatus.PENDIENTE).upper()
        if status_input not in dict(EtiquetaRFIDImpresion.Estatus.choices):
            status_input = EtiquetaRFIDImpresion.Estatus.PENDIENTE

        if etiquetas_input and len(etiquetas_input) != cantidad:
            raise ValidationError(
                "La cantidad de etiquetas no coincide con el arreglo enviado."
            )

        preview = servicio._build_label_preview(variante=variante, producto=producto)
        barcode_value_base = preview["barcode_value"]

        impresion = EtiquetaRFIDImpresion.objects.create(
            empresa_id=empresa.pk,
            sucursal_id=sucursal,
            usuario_id=getattr(user, "pk", None),
            producto=producto,
            producto_variante=variante,
            cantidad=cantidad,
            rfid_mode=rfid_mode,
            printer_name=printer_name,
            printer_address=printer_address,
            status=status_input,
            zpl_enviado=zpl_enviado,
            observaciones=observaciones,
        )

        if rfid_mode:
            if etiquetas_input:
                final_rows = []
                for idx, raw in enumerate(etiquetas_input, start=1):
                    epc = (raw.get("epc") or "").strip()
                    if not epc:
                        raise ValidationError(
                            f"La etiqueta {idx} no trae 'epc' válido."
                        )
                    final_rows.append(
                        EtiquetaRFIDDetalle(
                            impresion=impresion,
                            epc=epc.upper(),
                            barcode_value=(
                                raw.get("barcode_value") or barcode_value_base
                            ),
                            serial=raw.get("serial"),
                            estado=(
                                EtiquetaRFIDDetalle.Estado.IMPRESO
                                if status_input == EtiquetaRFIDImpresion.Estatus.EXITO
                                else EtiquetaRFIDDetalle.Estado.PENDIENTE
                            ),
                        )
                    )
                EtiquetaRFIDDetalle.objects.bulk_create(final_rows)
            else:
                generated = servicio._generate_epc_list(
                    cantidad, variante=variante, producto=producto
                )
                EtiquetaRFIDDetalle.objects.bulk_create(
                    [
                        EtiquetaRFIDDetalle(
                            impresion=impresion,
                            epc=row["epc"],
                            barcode_value=barcode_value_base,
                            serial=row["serial"],
                            estado=(
                                EtiquetaRFIDDetalle.Estado.IMPRESO
                                if status_input == EtiquetaRFIDImpresion.Estatus.EXITO
                                else EtiquetaRFIDDetalle.Estado.PENDIENTE
                            ),
                        )
                        for row in generated
                    ]
                )

        return impresion
