from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import ValidationError
from auditoria.models import AuditoriaEvento
from wms.models import Transferencia, TransferenciaDetalle
from inventarios.models import Existencia
from wms.utils.folios import generate_folio
from wms.services.existencia_service import ExistenciaService, SaldoExistenciaAlmacen
from wms.services.movimiento_inventario_service import MovimientoInventarioService
from wms.utils.decimales import normalizar_decimal


class TransferenciaService:
    _normalize = staticmethod(normalizar_decimal)

    @staticmethod
    @transaction.atomic
    def handle_store(data, user, request=None):
        almacen_origen = data["almacen_origen"]
        almacen_destino = data["almacen_destino"]
        empresa = user.empresa
        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError("El usuario no tiene una sucursal asignada.")

        # Validación de tenencia de los almacenes. Los campos almacen_origen/
        # almacen_destino del serializer no están acotados (queryset=Almacen.objects
        # .all()); sin esto un usuario podría referenciar almacenes de otra empresa o
        # de una sucursal a la que no tiene acceso. La lectura ya se acota en
        # TransferenciaViewSet.get_queryset(); esto cierra el lado de escritura. No se
        # exige que origen y destino compartan sucursal: una transferencia
        # inter-sucursal es legítima siempre que el usuario tenga acceso a ambas.
        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )

        # Empresa: si el almacén tiene empresa, debe coincidir con la del usuario (la
        # que se timbra en la transferencia). No hay caso legítimo cross-empresa.
        # Almacen.empresa es nullable; los almacenes sin empresa no se comprueban aquí
        # (mismo criterio que el perform_create de inventarios).
        if empresa is not None:
            if almacen_origen.empresa_id and almacen_origen.empresa_id != empresa.pk:
                raise ValidationError(
                    "El almacén de origen pertenece a una empresa distinta a la del usuario."
                )
            if almacen_destino.empresa_id and almacen_destino.empresa_id != empresa.pk:
                raise ValidationError(
                    "El almacén de destino pertenece a una empresa distinta a la del usuario."
                )

        # Sucursal: el usuario debe tener acceso a la sucursal de cada almacén.
        # Superuser y admin de empresa ven todas las sucursales de su empresa y se
        # saltan la comprobación (mismo criterio que get_queryset()). El conjunto
        # permitido son las sucursales del M2M user.sucursales más la sucursal por
        # defecto (con la que se timbra la transferencia), para no bloquear a usuarios
        # cuyo M2M esté vacío pero con sucursal_default asignada. Almacen.sucursal es
        # nullable; un almacén sin sucursal no se comprueba.
        if not es_staff:
            sucursales_permitidas = user.sucursales_permitidas()
            if (
                almacen_origen.sucursal_id
                and almacen_origen.sucursal_id not in sucursales_permitidas
            ):
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén de origen."
                )
            if (
                almacen_destino.sucursal_id
                and almacen_destino.sucursal_id not in sucursales_permitidas
            ):
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén de destino."
                )

        transferencia_detalle_rows = data.pop("transferencia_detalle")

        # 1. Validar inventario y repartir el movimiento (todavía sin escribir)
        #
        # El stock de un mismo producto/variante puede estar repartido en varias
        # filas de Existencia (una por ubicación), que es la topología normal de un
        # almacén con ubicaciones. Se valida contra la suma de todas ellas y no
        # contra la primera: mirar una sola fila rechazaba transferencias con stock
        # total suficiente. Un almacén sin ubicaciones (una sola fila) se comporta
        # igual que antes, porque la suma de una fila es esa fila.
        #
        # Origen y destino se llevan en acumuladores por clave de stock, no por
        # renglón: dos renglones de la misma clave (p.ej. dos tallas sin variante
        # del mismo producto) apuntan a las mismas filas de Existencia, y tratarlos
        # por separado hacía que la segunda escritura pisara a la primera.
        #
        # Se compara contra la existencia FÍSICA, no contra la disponible: cuando
        # la transferencia viene de un picking, las reservas ACTIVAS que la
        # respaldan son las de este mismo movimiento —ReservaInventarioService las
        # creó unas líneas antes, en la misma transacción— y restarlas aquí sería
        # descontarlas dos veces. La validación contra disponible ya la hizo la
        # etapa de reserva.
        saldos_origen = SaldoExistenciaAlmacen(almacen_origen, lock=True)
        destinos_por_clave = {}

        for row in transferencia_detalle_rows:
            producto = row.get("producto")
            producto_variante = row.get("producto_variante")
            cantidad = TransferenciaService._normalize(row["cantidad"])
            producto_id = producto.id if producto else producto_variante.id

            if not saldos_origen.filas(producto, producto_variante):
                raise ValidationError(
                    f"No hay existencia del producto/variante con id {producto_id} en el almacén de origen."
                )

            disponible = saldos_origen.disponible(producto, producto_variante)
            _asignaciones, faltante = saldos_origen.consumir(
                producto, producto_variante, cantidad
            )
            if faltante > Decimal("0"):
                raise ValidationError(
                    f"Inventario insuficiente del producto/variante con id {producto_id} "
                    f"(disponible={disponible}, solicitado={cantidad})."
                )

            clave = SaldoExistenciaAlmacen._clave(producto, producto_variante)
            if clave not in destinos_por_clave:
                existencia_destino = ExistenciaService.get_existencia(
                    almacen_destino,
                    producto,
                    producto_variante,
                )
                if existencia_destino is None:
                    existencia_destino = Existencia(
                        producto=producto,
                        producto_variante=producto_variante,
                        almacen=almacen_destino,
                        stock=0,
                        cantidad=Decimal("0"),
                    )
                else:
                    existencia_destino.cantidad = TransferenciaService._normalize(
                        existencia_destino.cantidad
                    )
                destinos_por_clave[clave] = existencia_destino
            destinos_por_clave[clave].cantidad += cantidad

        # 2. Crear transferencia
        transferencia = Transferencia.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            folio=generate_folio(empresa, sucursal, 'Transferencia'),
            usuario=user,
            **data,
        )

        # 3. Persistir existencias y preparar detalles
        #
        # Una escritura por fila con el saldo ya acumulado de todos los renglones
        # que compartían su clave. El reparto entre ubicaciones se hizo en la fase
        # 1, en orden de pk y sobre filas bloqueadas con select_for_update(),
        # mismo criterio que ReservaInventarioService.create_for_picking: la
        # reserva y el movimiento físico consumen las mismas filas en el mismo
        # orden.
        for existencia_origen, saldo_final in saldos_origen.filas_consumidas():
            existencia_origen.cantidad = saldo_final
            existencia_origen.save(update_fields=["cantidad"])

        for existencia_destino in destinos_por_clave.values():
            existencia_destino.save()

        TransferenciaDetalle.objects.bulk_create(
            [
                TransferenciaDetalle(transferencia=transferencia, **row)
                for row in transferencia_detalle_rows
            ]
        )

        # 4. Registrar movimientos
        MovimientoInventarioService.handle_store_for_transferencia(
            usuario=user,
            empresa=empresa,
            sucursal=sucursal,
            transferencia=transferencia,
            transferencia_detalle_rows=transferencia_detalle_rows,
        )

        # 5. Registrar evento de auditoría
        items_audit = []
        for row in transferencia_detalle_rows:
            producto = row.get("producto")
            producto_variante = row.get("producto_variante")
            items_audit.append(
                {
                    "producto_id": producto.pk if producto else None,
                    "producto_variante_id": producto_variante.pk if producto_variante else None,
                    "cantidad": str(TransferenciaService._normalize(row["cantidad"])),
                    "ubicacion_origen_id": getattr(row.get("ubicacion_origen"), "pk", None),
                    "ubicacion_destino_id": getattr(row.get("ubicacion_destino"), "pk", None),
                    "lote_id": getattr(row.get("lote"), "pk", None),
                    "serie_id": getattr(row.get("serie"), "pk", None),
                }
            )

        payload_base = {
            "almacen_origen_id": almacen_origen.pk,
            "almacen_destino_id": almacen_destino.pk,
            "sucursal_id": getattr(sucursal, "pk", None),
            "empresa_id": getattr(empresa, "pk", None),
            "folio": transferencia.folio,
            "items": items_audit,
        }

        ip = None
        user_agent = None
        if request is not None:
            meta = getattr(request, "META", None) or {}
            ip = (
                meta.get("HTTP_X_FORWARDED_FOR")
                or meta.get("REMOTE_ADDR")
                or None
            )
            user_agent = meta.get("HTTP_USER_AGENT") or None

        AuditoriaEvento.objects.create(
            empresa=empresa,
            usuario=user if getattr(user, "pk", None) else None,
            modulo="inventarios",
            accion="TRANSFERENCIA",
            tabla="existencias",
            id_registro=str(transferencia.pk),
            antes_json=payload_base,
            despues_json={**payload_base, "transferencia_id": transferencia.pk},
            ip=ip,
            user_agent=user_agent,
        )

        return transferencia
