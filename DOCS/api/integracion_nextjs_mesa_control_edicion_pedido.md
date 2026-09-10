# Integracion Next.js: Edicion de Pedido en Mesa de Control

## Flujo

## Acceso

- puede entrar `is_superuser`
- puede entrar `is_admin_empresa`
- puede entrar un usuario normal con rol activo de Mesa de Control
- backend identifica Mesa de Control por `clave_departamento="MESACONTROL"`
- tambien reconoce roles legacy como `codigo="MESACONTROL-0002"` o `nombre="Mesa-de-control"`
- si el usuario no cumple, responde:

```json
{
  "permiso": "Accion disponible solo para mesa de control."
}
```

### 1. Validar si el pedido `P` puede editarse

Llamar primero:

`GET /api/v1/ventas/pedidos/{id}/editar-mesa-control-contexto/`

### Si responde:

```json
{
  "editable": true,
  "modo": "estricto_contable_operativo",
  "requiere_ids_detalle": true,
  "bloqueos": []
}
```

Abrir pantalla de edicion.

### Si responde:

```json
{
  "editable": false,
  "codigo": "pedido_con_bloqueos",
  "bloqueos": []
}
```

No intentar guardar. Solo mostrar la lista de bloqueos.

## 2. Cargar pedido para poblar el formulario

Usar:

`GET /api/v1/ventas/pedidos/{id}/`

Importante:

- de ahi salen los `detalles[].id`
- esos `id` se tienen que mandar de regreso en el `POST`
- no inventar ids
- no eliminar renglones, tallas o servicios extras existentes desde frontend

## 3. Guardar cambios

Mandar:

`POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/`

Body ejemplo:

```json
{
  "pedido": {
    "sucursal": 1,
    "cliente": 15,
    "moneda": 1,
    "tipo_pedido": 2,
    "persona_pagos": "Pagos Mesa",
    "correo_facturas": "mesa@cliente.com",
    "telefono_pagos": "8111111111",
    "oc": "OC-123",
    "forma_pago": "03",
    "metodo_pago": "PUE",
    "uso_cfdi": "G03",
    "direccion_envio": "Calle Entrega 456",
    "subtotal": "250.00",
    "iva": 16,
    "gran_total": "290.00"
  },
  "detalle": [
    {
      "id": 123,
      "producto": 77,
      "precio_lista": "120.00",
      "precio_unitario": "110.00",
      "costo_unitario": "80.00",
      "tallas": [
        {
          "talla": 4,
          "cantidad": 3,
          "lleva_bordado": true,
          "bordado_config": {
            "ubicaciones": [{ "codigo": "PE" }]
          }
        }
      ]
    }
  ],
  "servicios_extras": [
    {
      "nombre": "Urgencia",
      "monto": "50.00",
      "cantidad": 2,
      "visible_en_factura": false
    }
  ]
}
```

## Reglas frontend

- `detalle[].id` es obligatorio para renglones existentes
- no quitar renglones existentes
- no quitar tallas existentes
- no quitar servicios extras existentes
- si hay bloqueos, no guardar
- primero contexto, luego edicion
- no asumir que solo `admin_empresa` puede editar; el usuario operativo de Mesa de Control tambien debe poder hacerlo

## Respuestas esperadas

### `200 OK`

```json
{
  "pedido": {},
  "cotizacion": {},
  "sincronizado": true,
  "modo": "estricto_contable_operativo"
}
```

### `409 Conflict`

```json
{
  "editable": false,
  "codigo": "pedido_con_bloqueos",
  "mensaje": "El pedido tiene documentos operativos/contables/logisticos ligados. Deben cancelarse o darse de baja manualmente antes de editarlo desde mesa de control.",
  "bloqueos": []
}
```

### `400 Bad Request`

Cuando el body viene mal armado o falta algo del payload.

## Que hace backend

- actualiza el `Pedido`
- sincroniza la `Cotizacion`
- actualiza `aprobado_snapshot`
- no toca inventario
- no permite edicion si hay documentos ligados

## Que bloquea la edicion

- factura emitida
- nota de credito ligada
- orden bordado activa
- orden reflejante activa
- orden corte activa
- orden produccion activa
- picking activo
- reservas activas
