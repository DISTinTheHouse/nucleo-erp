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

- [ ] ¿TERMINADO?
- [x] proveedores
- [x] direcciones_proveedor
- [x] ordenes_compra
- [ ] recepciones

_// omitir por ahora calidad, facturas proveedor y pagos //_

**_objetivo: Entrada formal de MP para tener impacto en inventario_**

## Fase 4 - Produccion (solo lo indispensable) |

(no empezar con todo producción)

- [ ] ¿TERMINADO?
- [ ] listas_materiales_bom
- [ ] ordenes_produccion
- [ ] consumos_produccion
- [ ] producto_terminado_entradas
- [ ] (meter seccion de corte)
- [ ] (opcion de meter seccion de proveedor o simplemente ligarlo)

**_Objetivo: Comsumir MP + Generar PT + ver transformación real_**

## Fase 5 - Clientes + pedidos (lado vendedor) |

- [ ] ¿TERMINADO?
- [ ] (cotizaciones en lugar de pedidos)
- [ ] clientes
- [ ] direcciones_cliente ← **sí, agrégar**
- [ ] pedidos
- [ ] pedido_detalle

**_Objetivo: Un vendedor pueda capturar pedidos reales_**

Recomendación: Pedidos simples, sin backorders, sin entregas parciales

## Fase 6 - Integración pedidos + inventarios + producción | HARIA FALTA CONSIDERAR CORTE

_// en esta fase el ERP ya debe sentirse "PRO"_

- TERMINADO
- [ ] Pedido de stock → baja inventario
- [ ] Pedido de fabricación → genera OP
- [ ] Pedido mixto → split automático

**_Objetivo: 'El PEDIDO' será nuestro 'DOCUMENTO MAESTRO' para detonar ordenes de producción,
ordenes de bordado, ordenes de compra, una factura, descuento de inventario_**
