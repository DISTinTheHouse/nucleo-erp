## Fase 0 - Requerimentos para fundaciones mínimas | CORE

- [x] ¿TERMINADO?
- [x] empresas
- [x] sucursales (opcion de mover a clientes)
- [x] almacenes
- [x] ubicaciones
- [x] usuarios / roles
- [x] (opcion de agregar uso-cfdi-sat, metodo-pago-sat, regiment-fiscal-sat, clave-productos-sat, clase-unidad-metodo-srt, y forma-pago-sat)
- [x] (opcion de agregar tipos de documentos) -> Implementado con Series y Folios (API Restructurada)
- [x] Refactorización de Arquitectura API: Separación de views/serializers en carpetas 'api' (nucleo, usuarios, seguridad)
- [x] Configuración de Producción: Variables de entorno seguras, DEBUG=False, y prefijos API verificados.
- [x] Documentación API actualizada con endpoints de Catalogo e Inventarios.

**objetivo: _Para que el sistema arranque, autentique y sepa donde existe el stock_**

## Fase 1 - Catalogo de productos | aqui ya podemos arrancar |

- [x] ¿TERMINADO?
- [x] categorias_producto Null
- [x] productos (+agregar catalogo del SAT)
- [x] colores
- [x] tallas
- [x] unidades_medida (CORE)
- [x] impuestos
- [x] tipo_producto: MP | PT | INSUMO | SERVICIO

**objetivo: _Tener productos reutilizables para compras, producción y ventas._**

## Fase 2 - Inventario base (sin implementar todo el WMS todavía) | 

- _unicamente : **entradas, salidas, ajustes**. para responder preguntas reales: "¿Cúanta tela hay? ¿Dónde está? ¿Por qué en esa ubicación?"_
- [x] ¿TERMINADO?
- [x] existencias
- [x] movimientos_inventario
- [x] movimiento_inventario_detalle
- [x] ajustes_inventario

## Fase 3 - Proveedores + compras básicas para alimentar MP |

_(sin materia prima no hay maquila)_

- [x] ¿TERMINADO?
- [x] proveedores
- [x] direcciones_proveedor
- [x] ordenes_compra
- [x] recepciones

_// omitir por ahora calidad, facturas proveedor y pagos //_

**_objetivo: Entrada formal de MP para tener impacto en inventario_**

## Fase 4 - Produccion (solo lo indispensable) |

(no empezar con todo producción)

- [x] ¿TERMINADO?
- [x] listas_materiales_bom
- [x] ordenes_produccion
- [x] consumos_produccion
- [-] producto_terminado_entradas

**_Objetivo: Comsumir MP + Generar PT + ver transformación real_**

### Nota operativa actual

- La entrada formal de producto terminado ya queda centralizada en `compras/Recepcion`.
- El onboarding `GET /api/v1/compras/recepciones/onboarding/` lista órdenes de compra y órdenes de producción.
- El `POST /api/v1/compras/recepciones/onboarding/` soporta origen `OC` u `OP`, actualiza `Existencia`, genera `MovimientoInventario` y liga `op` cuando la recepción viene de producción.
- `producto_terminado_entradas` queda como estructura redundante/no recomendada para seguir desarrollando.

## Fase 5 - Clientes + pedidos (lado vendedor) |

- [x] ¿TERMINADO?
- [x] cotizaciones
- [x] clientes
- [x] direcciones_cliente ← **sí, agregar**
- [x] pedidos
- [x] pedido_detalle

**_Objetivo: Un vendedor pueda capturar pedidos reales_**

Recomendación: Pedidos simples, sin backorders, sin entregas parciales

## Fase 6 - Integración pedidos + inventarios + producción | HARIA FALTA CONSIDERAR CORTE

_// en esta fase el ERP ya debe sentirse "PRO"_

- TERMINADO
- [x] Pedido de stock → baja inventario
- [x] Pedido de fabricación → genera OP
- [-] Pedido mixto → split automático (aun no se aclara la idea y no se ha implementado)

**_Objetivo: 'El PEDIDO' será nuestro 'DOCUMENTO MAESTRO' para detonar ordenes de producción,
ordenes de bordado, ordenes de compra, una factura, descuento de inventario_**

MESA DE CONTROL
      ↓
    PEDIDO
      ↓
┌─────┴──────────┐
↓                ↓
EXISTENCIAS      COMPRAS
↓                ↓
SURTIDO       RECEPCIÓN
↓                ↓
EMPAQUE        INVENTARIO
↓
ENVÍO

      └──── Producción cuando no hay existencia ────┐
                                                     ↓
                                               PRODUCCIÓN
                                                     ↓
                                                INVENTARIO

---

> **Leyenda:** `[x]` implementado con API/lógica · `[-]` parcial (existe pero incompleto) · `[ ]` pendiente.
> Las fases 7+ se verificaron contra el código (rama `feature/parcialidades-listado-pedidos`, octubre 2026). Un modelo sin endpoint **no** cuenta como implementado.

## Fase 7 - Compras completas + calidad |

_(retoma lo que en Fase 3 se dejó "para después")_

- [x] ¿TERMINADO?
- [x] órdenes de compra con aceptar / cancelar (`/api/v1/compras/ordenes/`)
- [x] fecha de entrega estimada vs. real y cantidad recibida por renglón (pendientes de entrega)
- [x] recepción con doble origen OC u OP (`/api/v1/compras/recepciones/onboarding/`)
- [x] inspección de calidad entre recepción y existencia (`/api/v1/compras/calidad-inspecciones/`): solo lo liberado o en concesión entra al almacén
- [-] lo rechazado o en cuarentena no entra al almacén, pero su destino aún no está definido
- [-] requisiciones → solicitudes de compra → cotizaciones de proveedor: el modelo existe, pero aún no hay API

**_Objetivo: que ninguna MP llegue a existencia sin pasar por recepción y calidad._**

## Fase 8 - Producción especializada |

- [x] ¿TERMINADO?
- [x] órdenes de bordado + avances + incidencias
- [x] órdenes de reflejante + avances + incidencias
- [x] órdenes de corte de manga
- [x] ruta crítica de la OP (`orden-produccion/ruta-critica`)
- [x] pedidos especiales (`/api/v1/produccion/pedidos-especiales/`)
- [x] creación de OP atómica con consumo automático de MP (falla si no hay stock)
- [ ] trazabilidad por lote / serie: los modelos `Lote` y `Serie` están vacíos, sin campos ni flujo
- [ ] trazabilidad por operario

## Fase 9 - Ventas / Mesa de control |

- [x] ¿TERMINADO?
- [x] cotizaciones → enviar a revisión → autorizar / rechazar → se copian a pedido
- [x] mesa de control: editar pedido, aceptar / rechazar cambios
- [x] programación de entregas del pedido (`pedidos/{id}/programar`) con parcialidades
- [x] consulta de disponibilidad de PT por producto / talla (`pedidos/{id}/stock-detalle`)
- [x] recompra: clonar un pedido en una cotización nueva (`pedidos/{id}/recomprar`)
- [ ] prospectos / oportunidades (CRM): el modelo existe, pero no hay API
- [ ] entregas y devoluciones de venta: el modelo existe, pero no hay API
- [ ] generación de PDFs (cotización, pedido): no existe en el backend
- [-] pedido mixto → split automático (sigue pendiente desde Fase 6)

## Fase 10 - WMS (almacén) |

- [x] ¿TERMINADO?
- [x] picking contra pedido (`/api/v1/wms/pickings/onboarding/`) con reservas de inventario
- [x] packing por cajas (`/api/v1/wms/packings/`)
- [x] despacho (`/api/v1/wms/despachos/`)
- [x] transferencias entre almacenes (`/api/v1/wms/transferencias/`)
- [x] reportes de existencias y movimientos por periodo (`/api/v1/inventarios/operaciones/`)
- [ ] conteo cíclico: el modelo existe, pero no hay API
- [ ] stock mínimo / punto de reorden / alertas de nivel: no existe el campo
- [ ] envíos de logística (`logistica.Envio`): el modelo existe, pero no hay API

## Fase 11 - RFID |

- [x] ¿TERMINADO?
- [x] impresión de etiquetas RFID: búsqueda, vista previa y registro de impresión (`/api/v1/wms/etiquetas-rfid/`)
- [x] lecturas del escáner: registrar, consultar, estadísticas y limpiar (`etiquetas-rfid/scans`, `scanner-stats`)
- [x] encuadre RFID en recepción de compras (`RecepcionRFIDEncuadre` / `RecepcionRFIDLectura`)
- [-] SDK de impresora: hoy se valida con el simulador (`/api/receive-scan/`); falta la prueba con hardware real

## Fase 12 - Cuentas por cobrar / por pagar |

- [x] ¿TERMINADO?
- [x] facturación de cliente, incluida la factura desde pedido (`facturas/desde-pedido`; un pedido solo se factura una vez)
- [x] registrar factura pendiente de cobro → crea Factura + CxC + póliza de ingreso automáticamente
- [x] cobros aplicados a CxC (generan movimiento bancario; se pueden cancelar)
- [x] notas de crédito aplicadas / canceladas contra CxC
- [x] facturas de proveedor → CxP (sin duplicados; cuentas congeladas una vez que tienen pagos)
- [x] pagos aplicados a CxP (generan movimiento bancario; se pueden cancelar)
- [x] alertas de mora por vencimiento (`alertas-mora/generar` + comando `generar_alertas_mora`)
- [x] dashboard financiero (`/api/v1/finanzas/dashboard/resumen`)

## Fase 13 - Bancos y contabilidad |

- [x] ¿TERMINADO?
- [x] bancos y cuentas bancarias
- [x] movimientos bancarios (registrar / revertir / resumen por cuenta)
- [x] conciliación bancaria: preparar → cerrar, contra el saldo en libros
- [x] plan de cuentas (`cuentas-contables`) y centros de costo
- [x] pólizas: validar cuadre (cargos = abonos), contabilizar, cancelar
- [-] pólizas automáticas: solo la factura pendiente de cobro genera póliza; cobros, pagos y bancos todavía no
- [ ] balance general / estado de resultados / balanza de comprobación
- [ ] exportación contable para auditoría externa

## Fase 14 - Facturación timbrada (SAT / CFDI 4.0) |

- [-] ¿TERMINADO?
- [x] catálogos SAT (régimen fiscal, uso CFDI, métodos y formas de pago) → `populate_sat_catalogs`
- [x] validación de RFC (checksum SAT) y alta de cliente en Facturama
- [x] productos en Facturama (`/api/v1/finanzas/facturama/productos/`)
- [x] emisión de CFDI vía Facturama + descarga de XML/PDF + acuse (`/api/v1/finanzas/facturama/cfdi/`)
- [ ] ligar el CFDI timbrado a la `Factura` interna: no se guarda el UUID; hoy la emisión es directa contra Facturama

## Transversal - Seguridad, auditoría y soporte |

- [x] multi-tenant: cada endpoint filtra por la empresa del usuario (404 si el registro es de otra empresa)
- [x] RBAC: roles, permisos y overrides de grant / deny por usuario
- [x] login con JWT en cookie + MFA (API) / sesión + 2FA + bloqueo por fuerza bruta (Core)
- [x] bitácora de auditoría (`AuditoriaEvento`) + log de API
- [x] búsqueda global filtrada por permisos (`/api/v1/search/`)
- [x] notificaciones en tiempo real (`/api/v1/notificaciones/stream`, leídas / sin leer)
- [x] recursos humanos: empleados, puestos, contratos, turnos, asistencias, vacaciones, nómina, evaluaciones, etc. (`/api/v1/hr/`)
- [x] asistente IA (OpenAI) + Google Calendar / Gmail vía OAuth (`/api/v1/ai/`)
- [x] CI: `check` + revisión de migraciones pendientes en cada PR; las migraciones a producción se corren en un job aparte (GitHub Actions)
- [ ] módulo QA: es un esqueleto, sin modelos