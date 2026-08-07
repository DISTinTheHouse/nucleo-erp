import logging
import secrets
import time
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.template.defaultfilters import truncatechars
from rest_framework.exceptions import APIException, ValidationError

from catalogo.models import Producto, ProductoVariante
from wms.models import EtiquetaRFIDDetalle, EtiquetaRFIDImpresion

logger = logging.getLogger(__name__)

# Intentos de regeneración ante colisión de EPC en la rama generada por backend.
MAX_INTENTOS_EPC = 3

# Filas por INSERT. Postgres no acota ``bulk_batch_size`` (sólo sqlite y oracle lo
# sobreescriben), así que sin esto Django manda un único INSERT con todo el lote:
# ``EtiquetaRFIDDetalle`` inserta 7 columnas y el serializer permite ``cantidad``
# hasta 10000, o sea 70000 parámetros, por encima del tope de 65535 del protocolo
# extendido de psycopg. Ese error es ``ProgrammingError``, no ``IntegrityError``,
# así que se escapaba de los handlers de abajo y salía como 500.
EPC_BATCH_SIZE = 1000


def _es_colision_epc(exc):
    """¿El ``IntegrityError`` viene del índice único de ``epc``?

    Los handlers de abajo sólo saben traducir esa colisión. Cualquier otra
    violación —una constraint futura sobre ``(impresion, serial)``, por ejemplo—
    se re-lanza tal cual en vez de disfrazarse de EPC duplicado y, en la rama
    generada, de disparar tres regeneraciones que no pueden arreglarla.
    """
    return "epc" in str(exc).lower()


class EtiquetaRFIDColision409(APIException):
    status_code = 409
    default_detail = "Uno o más códigos EPC ya están registrados."
    default_code = "etiqueta_rfid_colision"


class RFIDLabelService:
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
        """Genera ``cantidad`` EPCs de 24 hex (96 bits) para el lote.

        El total se mantiene en **24 hex** a propósito: 96 bits es el tamaño
        estándar del banco EPC de un tag Gen2 (SGTIN-96) y ``_build_zpl_rfid``
        escribe el dato con ``^RFW,E`` sin declarar ``^RS``, así que el printer
        asume el tamaño nativo del inlay. Ampliarlo a 112 bits (28 hex) fallaría
        la escritura contra un inlay de 96 bits —void físico, etiqueta RFID
        desperdiciada— y en este repo no hay ningún documento que fije el modelo
        de tag en uso, de modo que no es verificable desde código.

        Para bajar la probabilidad de colisión se **reasigna** el presupuesto en
        vez de crecerlo: ``ts_chunk`` cede 4 hex al sufijo aleatorio.

            antes:  prefix(8) + ts_chunk(8) + idx(4) + random(4) = 24
            ahora:  prefix(8) + ts_chunk(4) + idx(4) + random(8) = 24

        ``ts_chunk`` baja a 16 bits, así que cicla cada ~18.2 h en vez de cada
        ~136 años. Es un tradeoff deliberado: el modo de fallo real es el
        reintento/doble clic **dentro del mismo segundo**, donde ``ts_chunk``
        coincidía de todas formas y la colisión la decidía sólo el sufijo
        aleatorio —ahí pasa de 1/65_536 a 1/4.29e9 por etiqueta—. Para dos
        corridas separadas en el tiempo hace falta además caer en el mismo
        bucket de ~18.2 h, con lo que el neto queda en ~1/2.8e14: despreciable.
        """
        prefix = cls._generate_epc_base_prefix(variante=variante, producto=producto)
        ts_chunk = f"{int(time.time()) & 0xFFFF:04X}"
        result = []
        for idx in range(1, max(1, int(cantidad)) + 1):
            # ``idx`` se queda en 4 hex: el serializer topa ``cantidad`` en 10000
            # (0x2710) y no cabría en menos. El donante tenía que ser ``ts_chunk``.
            serial = f"{idx:04X}{secrets.token_hex(4).upper()}"
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
        lines.append("^RS8,E")
        lines.append("^RB96,,,1")
        lines.append("^RFW,E,,N")
        lines.append(f"^FD{epc}^FS")
        lines.append(
            f"^FO40,360^A0N,18,18^FDEPC: {epc[:16]}...{epc[-8:]}^FS"
        )
        lines.append("^XZ")
        return "\n".join(lines)

    @classmethod
    def _resolve_context(
        cls, user, variante_id=None, producto_id=None, exigir_sucursal=False
    ):
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
            if es_staff or sucursal_id in user.sucursales_permitidas():
                sucursal = sucursal_id

        # ``exigir_sucursal`` lo activa sólo la ruta de escritura. Una impresión
        # guardada con ``sucursal=NULL`` no volvía a verse nunca: el
        # ``get_queryset`` del viewset la acota con
        # ``sucursal_id__in=user.sucursales_permitidas()`` y en SQL un ``IN`` jamás
        # matchea ``NULL``, así que ``list`` la omitía y ``retrieve`` devolvía 404
        # —al propio autor incluido, pese al 201—. Se rechaza antes de escribir,
        # mismo criterio que ``TransferenciaService.handle_store()``.
        #
        # Se exige a **todos** los roles, staff incluido: un admin sin
        # ``sucursal_default`` generaba filas que ningún operador de piso podía
        # ver. Lo que ``es_staff`` sigue concediendo arriba es saltarse la
        # comprobación de pertenencia, no el derecho a omitir la sucursal.
        #
        # ``onboarding_preview`` no lo activa: no escribe nada y ``sucursal`` sólo
        # se refleja en el payload de respuesta, así que exigirla ahí rompería el
        # preview sin ninguna ganancia.
        if exigir_sucursal and sucursal is None:
            raise ValidationError(
                "El usuario no tiene una sucursal asignada. Configure su "
                "sucursal por defecto antes de imprimir etiquetas."
            )

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

        # Armar ZPL individual por etiqueta (RFID si rfid_mode, si no normal).
        # El frontend simplemente itera esta lista y envía cada zpl a Browser Print
        # —sin necesidad de reconstruir/reemplazar nada del ZPL—.
        zpls_individuales = []
        if rfid_mode:
            for row in etiquetas_metadata:
                zpls_individuales.append(
                    cls._build_zpl_rfid(
                        row["epc"],
                        variante=variante,
                        producto=producto,
                        barcode_value=barcode_value,
                    )
                )
        else:
            zpls_individuales = [zpl_normal] * len(etiquetas_metadata)

        zpl_rfid_first = ""
        if rfid_mode and etiquetas_metadata:
            zpl_rfid_first = zpls_individuales[0]

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
            "zpl_individual": zpls_individuales,
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
            exigir_sucursal=True,
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
                try:
                    EtiquetaRFIDDetalle.objects.bulk_create(
                        final_rows, batch_size=EPC_BATCH_SIZE
                    )
                except IntegrityError as exc:
                    if not _es_colision_epc(exc):
                        raise
                    # Red de seguridad de la carrera. ``EtiquetaRFIDCreateSerializer
                    # .validate`` ya rechaza con 400 los EPC repetidos en el payload
                    # y los que ya existen en BD, pero dos peticiones concurrentes
                    # con el mismo EPC pasan ambas ese ``filter()`` y una choca aquí.
                    #
                    # No se abre savepoint a propósito: no se reintenta ni se emite
                    # ningún query después, la excepción sale del ``atomic`` externo
                    # y hace rollback completo. Si alguna vez se agrega una consulta
                    # dentro de este ``except``, hay que envolver el ``bulk_create``
                    # en un ``with transaction.atomic()`` anidado primero —si no,
                    # reventará con ``TransactionManagementError``—.
                    logger.warning(
                        "Colisión de EPC en etiquetas enviadas por el cliente "
                        "(impresion=%s, empresa=%s, etiquetas=%s): carrera entre "
                        "peticiones concurrentes, el pre-chequeo no la atrapó.",
                        impresion.pk,
                        empresa.pk,
                        len(final_rows),
                    )
                    raise EtiquetaRFIDColision409()
            else:
                # Colisión en un EPC generado por backend: el cliente no mandó nada,
                # así que devolverle un error sería culparlo de un problema nuestro.
                # Se regenera con sufijo aleatorio nuevo y se reintenta.
                for intento in range(1, MAX_INTENTOS_EPC + 1):
                    generated = servicio._generate_epc_list(
                        cantidad, variante=variante, producto=producto
                    )
                    try:
                        # El ``atomic`` anidado es OBLIGATORIO, no cosmético: abre un
                        # savepoint. Sin él, al atrapar el ``IntegrityError`` la
                        # transacción externa queda marcada ``needs_rollback`` y el
                        # primer query del intento siguiente reventaría con
                        # ``TransactionManagementError`` —el modo de fallo clásico de
                        # este patrón—. Con savepoint sólo se deshace el intento
                        # fallido y el encabezado ya creado sobrevive.
                        with transaction.atomic():
                            EtiquetaRFIDDetalle.objects.bulk_create(
                                [
                                    EtiquetaRFIDDetalle(
                                        impresion=impresion,
                                        epc=row["epc"],
                                        barcode_value=barcode_value_base,
                                        serial=row["serial"],
                                        estado=(
                                            EtiquetaRFIDDetalle.Estado.IMPRESO
                                            if status_input
                                            == EtiquetaRFIDImpresion.Estatus.EXITO
                                            else EtiquetaRFIDDetalle.Estado.PENDIENTE
                                        ),
                                    )
                                    for row in generated
                                ],
                                batch_size=EPC_BATCH_SIZE,
                            )
                        break
                    except IntegrityError as exc:
                        if not _es_colision_epc(exc):
                            raise
                        if intento == MAX_INTENTOS_EPC:
                            logger.error(
                                "Colisión de EPC generado por backend agotó los %s "
                                "intentos (impresion=%s, empresa=%s, cantidad=%s). "
                                "Revisar entropía de _generate_epc_list.",
                                MAX_INTENTOS_EPC,
                                impresion.pk,
                                empresa.pk,
                                cantidad,
                            )
                            raise EtiquetaRFIDColision409(
                                "No fue posible generar códigos EPC únicos después "
                                f"de {MAX_INTENTOS_EPC} intentos. Reintente la "
                                "operación."
                            )
                        logger.warning(
                            "Colisión de EPC generado por backend en el intento %s/%s "
                            "(impresion=%s, empresa=%s, cantidad=%s): regenerando.",
                            intento,
                            MAX_INTENTOS_EPC,
                            impresion.pk,
                            empresa.pk,
                            cantidad,
                        )

        return impresion
