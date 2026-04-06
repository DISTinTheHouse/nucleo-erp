// ============================================================
// ERP (DBML)
// - SOLO nombres de tablas + IDs (PK/FK) con sus relaciones (Ref)
// - Cubre la columna vertebral completa y módulos del ERP
// ============================================================


// =========================
// 0) NÚCLEO / MULTI-TENANT
// =========================

Table empresas {
  id_empresa int [pk]
}

Table sucursales {
  id_sucursal int [pk]
  id_empresa int
}

Table departamentos {
  id_departamento int [pk]
  id_sucursal int
}

Table usuarios {
  id_usuario int [pk]
  id_empresa int
}

Table roles {
  id_rol int [pk]
  id_empresa int
}

Table permisos {
  id_permiso int [pk]
}

Table usuarios_roles {
  id_usuario int
  id_rol int
}

Table roles_permisos {
  id_rol int
  id_permiso int
}

// catálogos base (solo ids para relacionar)
Table monedas {
  id_moneda int [pk]
}

Table impuestos {
  id_impuesto int [pk]
}

Table unidades_medida {
  id_unidad_medida int [pk]
}

Table series_folios {
  id_serie_folio int [pk]
  id_sucursal int
}

// =========================
// 0.1v SAT (CATÁLOGOS GLOBALES + CONFIG POR EMPRESA)
// =========================

// --- Catálogos oficiales SAT (globales, NO dependen de empresa) ---

Table sat_uso_cfdi {
  id_sat_uso_cfdi int [pk]
}

Table sat_metodo_pago {
  id_sat_metodo_pago int [pk]
}

Table sat_forma_pago {
  id_sat_forma_pago int [pk]
}

Table sat_regimen_fiscal {
  id_sat_regimen_fiscal int [pk]
}

Table sat_clave_prodserv {
  id_sat_prodserv int [pk]
}

Table sat_clave_unidad {
  id_sat_unidad int [pk]
}

// --- Configuración / defaults por empresa (sí depende de empresa) ---

Table empresa_sat_config {
  id_empresa_sat_config int [pk]
  id_empresa int

  id_sat_regimen_fiscal_default int
  id_sat_uso_cfdi_default int
  id_sat_metodo_pago_default int
  id_sat_forma_pago_default int
}


// =========================
// 1) TERCEROS / MAESTROS
// =========================

// agregar tabla de direcciones 

Table clientes {
  id_cliente int [pk]
  id_empresa int

  id_sat_regimen_fiscal int
  id_sat_uso_cfdi int
}

Table contactos_cliente {
  id_contacto_cliente int [pk]
  id_cliente int
}

Table proveedores {
  id_proveedor int [pk]
  id_empresa int
}

Table contactos_proveedor {
  id_contacto_proveedor int [pk]
  id_proveedor int
}

Table transportistas {
  id_transportista int [pk]
  id_empresa int
}


// =========================
// 2) CATALOGO
// =========================

Table tipo_producto {
  id_tipo_producto int [pk]
}

Table categorias_producto {
  id_categoria_producto int [pk]
  id_empresa int
}

Table colores {
  id_color int [pk]
}

Table tallas {
  id_talla int [pk]
}

Table productos {
  id_producto int [pk]
  id_empresa int
  id_categoria_producto int
  id_unidad_medida int
  id_impuesto int
  id_sat_prodserv int
  id_sat_unidad int
  // opcional si decides normalizar el "tipo" (hoy en Django es CharField)
  id_tipo_producto int
}

Table variantes_producto {
  id_variante_producto int [pk]
  id_producto int
  id_empresa int
  id_color int
  id_talla int
}



// =========================
// 3) CRM / VENTAS
// =========================

// agregar tabla comisiones

Table prospectos {
  id_prospecto int [pk]
  id_empresa int
}

Table oportunidades {
  id_oportunidad int [pk]
  id_prospecto int
}

Table actividades_crm {
  id_actividad_crm int [pk]
  id_empresa int
  id_cliente int
  id_prospecto int
  id_oportunidad int
}

Table cotizaciones {
  id_cotizacion int [pk]
  id_empresa int
  id_cliente int
  id_oportunidad int
  id_moneda int
  serigrafia decimal(10,2)
  reflejante decimal(10,2)
}

Table cotizacion_detalle {
  id_cotizacion_detalle int [pk]
  id_cotizacion int
  id_producto int
}

// agregar id agente de ventas 

Table pedidos {
  id_pedido int [pk]
  id_empresa int
  id_sucursal int
  id_cliente int
  id_cotizacion int
  id_moneda int
  id_canal_venta int
  id_tienda int
  //
  id_tipo_pedido int // 1=Stock, 2=Fabricación, 3=Muestra, 4=Mixto
  estatus_pedido int // 1=Borrador, 2=Por Autorizar, 3=Autorizado, 4=En Proceso, 5=Cerrado
  serigrafia decimal(10,2)
  reflejante decimal(10,2)
}

Table pedido_detalle {
  id_pedido_detalle int [pk]
  id_pedido int
  id_producto int
}

// mesa de control
Table backorders {
  id_backorder int [pk]
  id_pedido int
  // funcion autorizar pedidos que cotizan
}

Table backorder_detalle {
  id_backorder_detalle int [pk]
  id_backorder int
  id_pedido_detalle int
}
//------------------

Table entregas {
  id_entrega int [pk]
  id_pedido int
}

Table entrega_detalle {
  id_entrega_detalle int [pk]
  id_entrega int
  id_pedido_detalle int
}

Table devoluciones {
  id_devolucion int [pk]
  id_entrega int
  id_pedido int
}

Table devolucion_detalle {
  id_devolucion_detalle int [pk]
  id_devolucion int
  id_entrega_detalle int
  id_pedido_detalle int
}


// =========================
// 4) FABRICACIÓN / PRODUCCIÓN
// =========================

Table listas_materiales_bom {
  id_bom int [pk]
  id_empresa int
  id_producto int // producto padre / terminado
}

Table bom_detalle {
  id_bom_detalle int [pk]
  id_bom int
  id_producto int // componente / materia prima
}

Table rutas_produccion {
  id_ruta_produccion int [pk]
  id_empresa int
  id_producto int
}

Table operaciones {
  id_operacion int [pk]
  id_ruta_produccion int
}

Table ordenes_produccion {
  id_op int [pk]
  id_empresa int
  id_sucursal int
  id_pedido int
  id_ruta_produccion int
}

Table op_detalle {
  id_op_detalle int [pk]
  id_op int
  id_pedido_detalle int
  id_producto int
}

Table consumos_produccion {
  id_consumo_produccion int [pk]
  id_op int
}

Table consumo_detalle {
  id_consumo_detalle int [pk]
  id_consumo_produccion int
  id_producto int
}

Table produccion_avances {
  id_produccion_avance int [pk]
  id_op int
}

Table produccion_avance_detalle {
  id_produccion_avance_detalle int [pk]
  id_produccion_avance int
  id_operacion int
}

Table producto_terminado_entradas {
  id_pt_entrada int [pk]
  id_op int
  id_almacen int
  id_ubicacion int
}


// =========================
// 5) INVENTARIOS (STOCK / MOVIMIENTOS / TRAZABILIDAD)
// =========================

// Table almacenes {
//   id_almacen int [pk]
//   id_sucursal int
// }

// Table ubicaciones {
//   id_ubicacion int [pk]
//   id_almacen int
// }

Table almacenes {
  id_almacen int [pk]
  id_empresa int
  id_sucursal int
  orden int // orden visual / operativo
}

Table ubicaciones {
  id_ubicacion int [pk]
  id_almacen integer
}

Table lotes {
  id_lote int [pk]
  id_producto int
}

Table series {
  id_serie int [pk]
  id_producto int
}

Table existencias {
  id_existencia int [pk]
  id_producto int
  id_almacen int
  id_ubicacion int
  id_lote int
  id_serie int
}

Table movimientos_inventario {
  id_movimiento_inventario int [pk]
  id_empresa int
  id_sucursal int
  id_pedido int
  id_entrega int
  id_devolucion int
  id_recepcion int
  id_transferencia int
  id_ajuste int
  id_op int
}

Table movimiento_inventario_detalle {
  id_movimiento_detalle int [pk]
  id_movimiento_inventario int
  id_producto int
  id_ubicacion_origen int
  id_ubicacion_destino int
  id_lote int
  id_serie int
}

Table ajustes_inventario {
  id_ajuste int [pk]
  id_empresa int
  id_sucursal int
  id_almacen int
}

Table ajuste_detalle {
  id_ajuste_detalle int [pk]
  id_ajuste int
  id_producto int
  id_ubicacion int
  id_lote int
  id_serie int
}

// reserva de inventario (variante típica)
Table inventario_reservas {
  id_reserva int [pk]
  id_pedido_detalle int
  id_existencia int
}

// costos (kardex / costo promedio / etc.)
Table costos_inventario {
  id_costo_inventario int [pk]
  id_empresa int
  id_producto int
}

Table costo_detalle {
  id_costo_detalle int [pk]
  id_costo_inventario int
  id_movimiento_inventario int
  id_factura int
  id_factura_proveedor int
  id_recepcion int
}


// Tablas para RFID

// =========================
// 6) WMS (ALMACÉN: PICK/PACK/SHIP + CONTEOS + TRANSFER) ADMINISTAR ALMACEN
// =========================

Table picking {
  id_picking int [pk]
  id_pedido int
}

Table picking_detalle {
  id_picking_detalle int [pk]
  id_picking int
  id_pedido_detalle int
}

// ver que tablas agregar para distribuir pedido
// opcion a agregar O.B. o apartados
Table packing {
  id_packing int [pk]
  id_picking int
}

Table packing_detalle {
  id_packing_detalle int [pk]
  id_packing int
  id_picking_detalle int
}

// verificar funcion despacho y mod. nombre
Table despachos {
  id_despacho int [pk]
  id_packing int
  id_envio int
}

Table despacho_detalle {
  id_despacho_detalle int [pk]
  id_despacho int
  id_packing_detalle int
}

Table conteos_ciclicos {
  id_conteo_ciclico int [pk]
  id_almacen int
}

Table conteo_ciclico_detalle {
  id_conteo_ciclico_detalle int [pk]
  id_conteo_ciclico int
  id_existencia int
}

Table transferencias {
  id_transferencia int [pk]
  id_empresa int
  id_sucursal int
}

Table transferencia_detalle {
  id_transferencia_detalle int [pk]
  id_transferencia int
  id_producto int
  id_ubicacion_origen int
  id_ubicacion_destino int
  id_lote int
  id_serie int
}


// =========================
// 7) SCM + COMPRAS (REQ / RFQ / OC / RECEPCIÓN / CALIDAD)
// =========================

Table requisiciones {
  id_requisicion int [pk]
  id_empresa int
  id_sucursal int
  id_departamento int
}

Table requisicion_detalle {
  id_requisicion_detalle int [pk]
  id_requisicion int
  id_producto int
}

Table cotizaciones_proveedor {
  id_cotizacion_proveedor int [pk]
  id_proveedor int
  id_requisicion int
  id_moneda int
}

Table cotizacion_proveedor_detalle {
  id_cotizacion_proveedor_detalle int [pk]
  id_cotizacion_proveedor int
  id_requisicion_detalle int
  id_producto int
}

Table solicitudes_compra {
  id_solicitud_compra int [pk]
  id_empresa int
  id_sucursal int
  id_departamento int
  id_requisicion int
}

Table solicitud_compra_detalle {
  id_solicitud_compra_detalle int [pk]
  id_solicitud_compra int
  id_producto int
  id_requisicion_detalle int
}

// agregar id pedido | verificar con Orden de producción
Table ordenes_compra {
  id_oc int [pk]
  id_empresa int
  id_sucursal int
  id_proveedor int
  id_solicitud_compra int
  id_moneda int
}

Table orden_compra_detalle {
  id_oc_detalle int [pk]
  id_oc int
  id_producto int
  id_solicitud_compra_detalle int
  id_requisicion_detalle int
}

Table recepciones {
  id_recepcion int [pk]
  id_oc int
  id_almacen int
}

Table recepcion_detalle {
  id_recepcion_detalle int [pk]
  id_recepcion int
  id_oc_detalle int
  id_producto int
  id_ubicacion int
  id_lote int
  id_serie int
}

Table calidad_inspecciones {
  id_inspeccion_calidad int [pk]
  id_recepcion int
}

Table calidad_inspeccion_detalle {
  id_inspeccion_calidad_detalle int [pk]
  id_inspeccion_calidad int
  id_recepcion_detalle int
}

Table envios_proveedor {
  id_envio_proveedor int [pk]
  id_proveedor int
  id_transportista int
  id_oc int
}


// =========================
// 8) LOGÍSTICA / ENVIOS A CLIENTE
// =========================

// agregar O.C & O.P.
Table envios {
  id_envio int [pk]
  id_empresa int
  id_sucursal int
  id_pedido int
  id_transportista int
}

Table envio_detalle {
  id_envio_detalle int [pk]
  id_envio int
  id_entrega int
}


// =========================
// 9) FINANZAS (GL/AP/AR/BANCOS)
// =========================

Table cuentas_contables {
  id_cuenta_contable int [pk]
  id_empresa int
}

Table centros_costo {
  id_centro_costo int [pk]
  id_empresa int
}

Table polizas {
  id_poliza int [pk]
  id_empresa int
  id_sucursal int
}

Table poliza_detalle {
  id_poliza_detalle int [pk]
  id_poliza int
  id_cuenta_contable int
  id_centro_costo int
  // vínculos opcionales típicos para trazabilidad contable:
  id_factura int
  id_factura_proveedor int
  id_pago int
  id_cobro int
  id_nomina int
  id_movimiento_bancario int
}

Table bancos {
  id_banco int [pk]
  id_empresa int
}

Table cuentas_bancarias {
  id_cuenta_bancaria int [pk]
  id_banco int
  id_moneda int
}

Table movimientos_bancarios {
  id_movimiento_bancario int [pk]
  id_cuenta_bancaria int
  id_pago int
  id_cobro int
}

Table conciliaciones_bancarias {
  id_conciliacion int [pk]
  id_cuenta_bancaria int
}

Table conciliacion_detalle {
  id_conciliacion_detalle int [pk]
  id_conciliacion int
  id_movimiento_bancario int
}

// AR (CxC)
Table cuentas_por_cobrar {
  id_cxc int [pk]
  id_cliente int
  id_factura int
}

Table cobros {
  id_cobro int [pk]
  id_cliente int
  id_cuenta_bancaria int
}

Table cobro_detalle {
  id_cobro_detalle int [pk]
  id_cobro int
  id_cxc int
}

// AP (CxP)
Table cuentas_por_pagar {
  id_cxp int [pk]
  id_proveedor int
  id_factura_proveedor int
}

Table pagos {
  id_pago int [pk]
  id_proveedor int
  id_cuenta_bancaria int
}

Table pago_detalle {
  id_pago_detalle int [pk]
  id_pago int
  id_cxp int
}

// Facturación (ventas)
Table facturas {
  id_factura int [pk]
  id_empresa int
  id_sucursal int
  id_cliente int
  id_pedido int
  id_moneda int
}

Table factura_detalle {
  id_factura_detalle int [pk]
  id_factura int
  id_pedido_detalle int
  id_producto int
}

// información SAT (verificar catalogos pendientes admin)
// IMPORTANTE

// agregar id_cliente para ligarlo
Table notas_credito {
  id_nota_credito int [pk]
  id_factura int
  id_cliente int
}

Table nota_credito_detalle {
  id_nota_credito_detalle int [pk]
  id_nota_credito int
  id_factura_detalle int
}

// Facturación proveedor (compras)
// verificar opción a que el proveedor pueda subir su factura xml
Table facturas_proveedor {
  id_factura_proveedor int [pk]
  id_empresa int
  id_sucursal int
  id_proveedor int
  id_oc int
  id_recepcion int
  id_moneda int
}

Table factura_proveedor_detalle {
  id_factura_proveedor_detalle int [pk]
  id_factura_proveedor int
  id_oc_detalle int
  id_recepcion_detalle int
  id_producto int
}


// =========================
// 10) PROYECTOS (PSA)
// =========================

Table proyectos {
  id_proyecto int [pk]
  id_empresa int
  id_cliente int
}

Table proyecto_tareas {
  id_proyecto_tarea int [pk]
  id_proyecto int
}

Table proyecto_asignaciones {
  id_proyecto_asignacion int [pk]
  id_proyecto_tarea int
  id_empleado int
}

Table proyecto_horas {
  id_proyecto_hora int [pk]
  id_proyecto_tarea int
  id_empleado int
}

Table proyecto_costos {
  id_proyecto_costo int [pk]
  id_proyecto int
  id_centro_costo int
}

Table proyecto_facturacion {
  id_proyecto_facturacion int [pk]
  id_proyecto int
  id_factura int
}


// =========================
// 11) E-COMMERCE (FRONT/BACK)
// =========================

Table canales_venta {
  id_canal_venta int [pk]
  id_empresa int
}

Table tiendas {
  id_tienda int [pk]
  id_empresa int
  id_canal_venta int
}

Table catalogo_web {
  id_catalogo_web int [pk]
  id_tienda int
}

Table catalogo_web_detalle {
  id_catalogo_web_detalle int [pk]
  id_catalogo_web int
  id_producto int
}

Table carritos {
  id_carrito int [pk]
  id_tienda int
  id_cliente int
}

Table carrito_detalle {
  id_carrito_detalle int [pk]
  id_carrito int
  id_producto int
}

Table pagos_online {
  id_pago_online int [pk]
  id_tienda int
  id_pedido int
}

Table transacciones_pago {
  id_transaccion_pago int [pk]
  id_pago_online int
}


// =========================
// 12) MARKETING AUTOMATION
// =========================

Table segmentos {
  id_segmento int [pk]
  id_empresa int
}

Table segmento_clientes {
  id_segmento_cliente int [pk]
  id_segmento int
  id_cliente int
}

Table campanas {
  id_campana int [pk]
  id_empresa int
}

Table campana_detalle {
  id_campana_detalle int [pk]
  id_campana int
  id_segmento int
}

Table leads {
  id_lead int [pk]
  id_empresa int
  id_prospecto int
}

Table lead_origen {
  id_lead_origen int [pk]
  id_lead int
}

Table interacciones {
  id_interaccion int [pk]
  id_empresa int
  id_cliente int
  id_lead int
  id_campana int
}

Table interaccion_detalle {
  id_interaccion_detalle int [pk]
  id_interaccion int
}

Table automatizaciones {
  id_automatizacion int [pk]
  id_empresa int
}

Table automatizacion_pasos {
  id_automatizacion_paso int [pk]
  id_automatizacion int
  id_campana int
  id_interaccion int
}


// =========================
// 13) HR + WFM (ASISTENCIA / HORAS / NOMINA / PRODUCTIVIDAD)
// =========================

Table puestos {
  id_puesto int [pk]
  id_empresa int
}

Table empleados {
  id_empleado int [pk]
  id_empresa int
  id_sucursal int
  id_departamento int
  id_puesto int
}

Table contratos {
  id_contrato int [pk]
  id_empleado int
}

Table turnos {
  id_turno int [pk]
  id_empresa int
}

Table calendarios {
  id_calendario int [pk]
  id_turno int
}

Table asistencias {
  id_asistencia int [pk]
  id_empleado int
  id_turno int
}

Table control_horas {
  id_control_horas int [pk]
  id_empleado int
  id_asistencia int
  id_proyecto int
  id_op int
}

Table vacaciones {
  id_vacacion int [pk]
  id_empleado int
}

Table permisos_ausencias {
  id_permiso_ausencia int [pk]
  id_empleado int
}

Table incidencias {
  id_incidencia int [pk]
  id_empleado int
}

Table evaluaciones {
  id_evaluacion int [pk]
  id_empleado int
}

Table capacitaciones {
  id_capacitacion int [pk]
  id_empleado int
}

Table nominas {
  id_nomina int [pk]
  id_empresa int
  id_sucursal int
}

Table nomina_detalle {
  id_nomina_detalle int [pk]
  id_nomina int
  id_empleado int
}

Table productividad {
  id_productividad int [pk]
  id_empresa int
  id_departamento int
}

Table productividad_detalle {
  id_productividad_detalle int [pk]
  id_productividad int
  id_empleado int
  id_op int
  id_proyecto int
}


// ============================================================
// RELACIONES
// ============================================================

// Núcleo
Ref: sucursales.id_empresa > empresas.id_empresa
Ref: departamentos.id_sucursal > sucursales.id_sucursal
Ref: usuarios.id_empresa > empresas.id_empresa
Ref: roles.id_empresa > empresas.id_empresa
Ref: usuarios_roles.id_usuario > usuarios.id_usuario
Ref: usuarios_roles.id_rol > roles.id_rol
Ref: roles_permisos.id_rol > roles.id_rol
Ref: roles_permisos.id_permiso > permisos.id_permiso
Ref: series_folios.id_sucursal > sucursales.id_sucursal
// SAT (catálogos globales + config empresa)
Ref: empresa_sat_config.id_empresa > empresas.id_empresa

Ref: empresa_sat_config.id_sat_regimen_fiscal_default > sat_regimen_fiscal.id_sat_regimen_fiscal
Ref: empresa_sat_config.id_sat_uso_cfdi_default > sat_uso_cfdi.id_sat_uso_cfdi
Ref: empresa_sat_config.id_sat_metodo_pago_default > sat_metodo_pago.id_sat_metodo_pago
Ref: empresa_sat_config.id_sat_forma_pago_default > sat_forma_pago.id_sat_forma_pago

Ref: productos.id_sat_prodserv > sat_clave_prodserv.id_sat_prodserv
Ref: productos.id_sat_unidad > sat_clave_unidad.id_sat_unidad

Ref: clientes.id_sat_regimen_fiscal > sat_regimen_fiscal.id_sat_regimen_fiscal
Ref: clientes.id_sat_uso_cfdi > sat_uso_cfdi.id_sat_uso_cfdi

// Maestros terceros
Ref: clientes.id_empresa > empresas.id_empresa
Ref: contactos_cliente.id_cliente > clientes.id_cliente
Ref: proveedores.id_empresa > empresas.id_empresa
Ref: contactos_proveedor.id_proveedor > proveedores.id_proveedor
Ref: transportistas.id_empresa > empresas.id_empresa

// Categorías
Ref: categorias_producto.id_empresa > empresas.id_empresa

// Productos
Ref: productos.id_empresa > empresas.id_empresa
Ref: productos.id_categoria_producto > categorias_producto.id_categoria_producto
Ref: productos.id_unidad_medida > unidades_medida.id_unidad_medida
Ref: productos.id_impuesto > impuestos.id_impuesto
// Ref: productos.id_sat_prodserv > sat_clave_prodserv.id_sat_prodserv
// Ref: productos.id_sat_unidad > sat_clave_unidad.id_sat_unidad
Ref: productos.id_tipo_producto > tipo_producto.id_tipo_producto  // si lo normalizas

// Variantes
Ref: variantes_producto.id_producto > productos.id_producto
Ref: variantes_producto.id_empresa > empresas.id_empresa
Ref: variantes_producto.id_color > colores.id_color
Ref: variantes_producto.id_talla > tallas.id_talla

// CRM / Ventas
Ref: prospectos.id_empresa > empresas.id_empresa
Ref: oportunidades.id_prospecto > prospectos.id_prospecto
Ref: actividades_crm.id_empresa > empresas.id_empresa
Ref: actividades_crm.id_cliente > clientes.id_cliente
Ref: actividades_crm.id_prospecto > prospectos.id_prospecto
Ref: actividades_crm.id_oportunidad > oportunidades.id_oportunidad

Ref: cotizaciones.id_empresa > empresas.id_empresa
Ref: cotizaciones.id_cliente > clientes.id_cliente
Ref: cotizaciones.id_oportunidad > oportunidades.id_oportunidad
Ref: cotizaciones.id_moneda > monedas.id_moneda
Ref: cotizacion_detalle.id_cotizacion > cotizaciones.id_cotizacion
Ref: cotizacion_detalle.id_producto > productos.id_producto

// OM: pedidos / entregas / devoluciones
Ref: pedidos.id_empresa > empresas.id_empresa
Ref: pedidos.id_sucursal > sucursales.id_sucursal
Ref: pedidos.id_cliente > clientes.id_cliente
Ref: pedidos.id_cotizacion > cotizaciones.id_cotizacion
Ref: pedidos.id_moneda > monedas.id_moneda

Ref: pedido_detalle.id_pedido > pedidos.id_pedido
Ref: pedido_detalle.id_producto > productos.id_producto

Ref: backorders.id_pedido > pedidos.id_pedido
Ref: backorder_detalle.id_backorder > backorders.id_backorder
Ref: backorder_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle

Ref: entregas.id_pedido > pedidos.id_pedido
Ref: entrega_detalle.id_entrega > entregas.id_entrega
Ref: entrega_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle

Ref: devoluciones.id_entrega > entregas.id_entrega
Ref: devoluciones.id_pedido > pedidos.id_pedido
Ref: devolucion_detalle.id_devolucion > devoluciones.id_devolucion
Ref: devolucion_detalle.id_entrega_detalle > entrega_detalle.id_entrega_detalle
Ref: devolucion_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle

// Fabricación
Ref: listas_materiales_bom.id_empresa > empresas.id_empresa
Ref: listas_materiales_bom.id_producto > productos.id_producto
Ref: bom_detalle.id_bom > listas_materiales_bom.id_bom
Ref: bom_detalle.id_producto > productos.id_producto

Ref: rutas_produccion.id_empresa > empresas.id_empresa
Ref: rutas_produccion.id_producto > productos.id_producto
Ref: operaciones.id_ruta_produccion > rutas_produccion.id_ruta_produccion

Ref: ordenes_produccion.id_empresa > empresas.id_empresa
Ref: ordenes_produccion.id_sucursal > sucursales.id_sucursal
Ref: ordenes_produccion.id_pedido > pedidos.id_pedido
Ref: ordenes_produccion.id_ruta_produccion > rutas_produccion.id_ruta_produccion

Ref: op_detalle.id_op > ordenes_produccion.id_op
Ref: op_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle
Ref: op_detalle.id_producto > productos.id_producto

Ref: consumos_produccion.id_op > ordenes_produccion.id_op
Ref: consumo_detalle.id_consumo_produccion > consumos_produccion.id_consumo_produccion
Ref: consumo_detalle.id_producto > productos.id_producto

Ref: produccion_avances.id_op > ordenes_produccion.id_op
Ref: produccion_avance_detalle.id_produccion_avance > produccion_avances.id_produccion_avance
Ref: produccion_avance_detalle.id_operacion > operaciones.id_operacion

// Inventario base
Ref: almacenes.id_empresa > empresas.id_empresa
Ref: almacenes.id_sucursal > sucursales.id_sucursal
Ref: ubicaciones.id_almacen > almacenes.id_almacen
Ref: lotes.id_producto > productos.id_producto
Ref: series.id_producto > productos.id_producto

Ref: existencias.id_producto > productos.id_producto
Ref: existencias.id_almacen > almacenes.id_almacen
Ref: existencias.id_ubicacion > ubicaciones.id_ubicacion
Ref: existencias.id_lote > lotes.id_lote
Ref: existencias.id_serie > series.id_serie

// Movimientos inventario (columna vertebral con documentos)
Ref: movimientos_inventario.id_empresa > empresas.id_empresa
Ref: movimientos_inventario.id_sucursal > sucursales.id_sucursal
Ref: movimientos_inventario.id_pedido > pedidos.id_pedido
Ref: movimientos_inventario.id_entrega > entregas.id_entrega
Ref: movimientos_inventario.id_devolucion > devoluciones.id_devolucion
Ref: movimientos_inventario.id_op > ordenes_produccion.id_op
Ref: movimientos_inventario.id_transferencia > transferencias.id_transferencia
Ref: movimientos_inventario.id_ajuste > ajustes_inventario.id_ajuste

Ref: movimiento_inventario_detalle.id_movimiento_inventario > movimientos_inventario.id_movimiento_inventario
Ref: movimiento_inventario_detalle.id_producto > productos.id_producto
Ref: movimiento_inventario_detalle.id_ubicacion_origen > ubicaciones.id_ubicacion
Ref: movimiento_inventario_detalle.id_ubicacion_destino > ubicaciones.id_ubicacion
Ref: movimiento_inventario_detalle.id_lote > lotes.id_lote
Ref: movimiento_inventario_detalle.id_serie > series.id_serie

// Ajustes inventario
Ref: ajustes_inventario.id_empresa > empresas.id_empresa
Ref: ajustes_inventario.id_sucursal > sucursales.id_sucursal
Ref: ajustes_inventario.id_almacen > almacenes.id_almacen
Ref: ajuste_detalle.id_ajuste > ajustes_inventario.id_ajuste
Ref: ajuste_detalle.id_producto > productos.id_producto
Ref: ajuste_detalle.id_ubicacion > ubicaciones.id_ubicacion
Ref: ajuste_detalle.id_lote > lotes.id_lote
Ref: ajuste_detalle.id_serie > series.id_serie

// Reservas (variante típica)
Ref: inventario_reservas.id_pedido_detalle > pedido_detalle.id_pedido_detalle
Ref: inventario_reservas.id_existencia > existencias.id_existencia

// Costos inventario (trazabilidad)
Ref: costos_inventario.id_empresa > empresas.id_empresa
Ref: costos_inventario.id_producto > productos.id_producto
Ref: costo_detalle.id_costo_inventario > costos_inventario.id_costo_inventario
Ref: costo_detalle.id_movimiento_inventario > movimientos_inventario.id_movimiento_inventario
Ref: costo_detalle.id_recepcion > recepciones.id_recepcion
Ref: costo_detalle.id_factura > facturas.id_factura
Ref: costo_detalle.id_factura_proveedor > facturas_proveedor.id_factura_proveedor

// WMS
Ref: picking.id_pedido > pedidos.id_pedido
Ref: picking_detalle.id_picking > picking.id_picking
Ref: picking_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle

Ref: packing.id_picking > picking.id_picking
Ref: packing_detalle.id_packing > packing.id_packing
Ref: packing_detalle.id_picking_detalle > picking_detalle.id_picking_detalle

// WMS -> despacho puede apuntar a envío
Ref: despachos.id_packing > packing.id_packing
Ref: despacho_detalle.id_despacho > despachos.id_despacho
Ref: despacho_detalle.id_packing_detalle > packing_detalle.id_packing_detalle

// Conteos cíclicos -> Existencias (para auditoría/ajustes)
Ref: conteos_ciclicos.id_almacen > almacenes.id_almacen
Ref: conteo_ciclico_detalle.id_conteo_ciclico > conteos_ciclicos.id_conteo_ciclico
Ref: conteo_ciclico_detalle.id_existencia > existencias.id_existencia

// Transferencias internas (WMS) -> Ubicaciones/Lotes/Series
Ref: transferencias.id_empresa > empresas.id_empresa
Ref: transferencias.id_sucursal > sucursales.id_sucursal
Ref: transferencia_detalle.id_transferencia > transferencias.id_transferencia
Ref: transferencia_detalle.id_producto > productos.id_producto
Ref: transferencia_detalle.id_ubicacion_origen > ubicaciones.id_ubicacion
Ref: transferencia_detalle.id_ubicacion_destino > ubicaciones.id_ubicacion
Ref: transferencia_detalle.id_lote > lotes.id_lote
Ref: transferencia_detalle.id_serie > series.id_serie

// SCM / Compras
Ref: requisiciones.id_empresa > empresas.id_empresa
Ref: requisiciones.id_sucursal > sucursales.id_sucursal
Ref: requisiciones.id_departamento > departamentos.id_departamento
Ref: requisicion_detalle.id_requisicion > requisiciones.id_requisicion
Ref: requisicion_detalle.id_producto > productos.id_producto

Ref: cotizaciones_proveedor.id_proveedor > proveedores.id_proveedor
Ref: cotizaciones_proveedor.id_requisicion > requisiciones.id_requisicion
Ref: cotizaciones_proveedor.id_moneda > monedas.id_moneda
Ref: cotizacion_proveedor_detalle.id_cotizacion_proveedor > cotizaciones_proveedor.id_cotizacion_proveedor
Ref: cotizacion_proveedor_detalle.id_requisicion_detalle > requisicion_detalle.id_requisicion_detalle
Ref: cotizacion_proveedor_detalle.id_producto > productos.id_producto

Ref: solicitudes_compra.id_empresa > empresas.id_empresa
Ref: solicitudes_compra.id_sucursal > sucursales.id_sucursal
Ref: solicitudes_compra.id_departamento > departamentos.id_departamento
Ref: solicitudes_compra.id_requisicion > requisiciones.id_requisicion
Ref: solicitud_compra_detalle.id_solicitud_compra > solicitudes_compra.id_solicitud_compra
Ref: solicitud_compra_detalle.id_producto > productos.id_producto
Ref: solicitud_compra_detalle.id_requisicion_detalle > requisicion_detalle.id_requisicion_detalle

Ref: ordenes_compra.id_empresa > empresas.id_empresa
Ref: ordenes_compra.id_sucursal > sucursales.id_sucursal
Ref: ordenes_compra.id_proveedor > proveedores.id_proveedor
Ref: ordenes_compra.id_solicitud_compra > solicitudes_compra.id_solicitud_compra
Ref: ordenes_compra.id_moneda > monedas.id_moneda

Ref: orden_compra_detalle.id_oc > ordenes_compra.id_oc
Ref: orden_compra_detalle.id_producto > productos.id_producto
Ref: orden_compra_detalle.id_solicitud_compra_detalle > solicitud_compra_detalle.id_solicitud_compra_detalle
Ref: orden_compra_detalle.id_requisicion_detalle > requisicion_detalle.id_requisicion_detalle

Ref: recepciones.id_oc > ordenes_compra.id_oc
Ref: recepciones.id_almacen > almacenes.id_almacen
Ref: recepcion_detalle.id_recepcion > recepciones.id_recepcion
Ref: recepcion_detalle.id_oc_detalle > orden_compra_detalle.id_oc_detalle
Ref: recepcion_detalle.id_producto > productos.id_producto
Ref: recepcion_detalle.id_ubicacion > ubicaciones.id_ubicacion
Ref: recepcion_detalle.id_lote > lotes.id_lote
Ref: recepcion_detalle.id_serie > series.id_serie

Ref: calidad_inspecciones.id_recepcion > recepciones.id_recepcion
Ref: calidad_inspeccion_detalle.id_inspeccion_calidad > calidad_inspecciones.id_inspeccion_calidad
Ref: calidad_inspeccion_detalle.id_recepcion_detalle > recepcion_detalle.id_recepcion_detalle

Ref: envios_proveedor.id_proveedor > proveedores.id_proveedor
Ref: envios_proveedor.id_transportista > transportistas.id_transportista
Ref: envios_proveedor.id_oc > ordenes_compra.id_oc

// Envíos cliente
Ref: envios.id_empresa > empresas.id_empresa
Ref: envios.id_sucursal > sucursales.id_sucursal
Ref: envios.id_pedido > pedidos.id_pedido
Ref: envios.id_transportista > transportistas.id_transportista
Ref: envio_detalle.id_envio > envios.id_envio
Ref: envio_detalle.id_entrega > entregas.id_entrega

// Finanzas (GL/AP/AR/Bancos)
Ref: cuentas_contables.id_empresa > empresas.id_empresa
Ref: centros_costo.id_empresa > empresas.id_empresa

Ref: polizas.id_empresa > empresas.id_empresa
Ref: polizas.id_sucursal > sucursales.id_sucursal
Ref: poliza_detalle.id_poliza > polizas.id_poliza
Ref: poliza_detalle.id_cuenta_contable > cuentas_contables.id_cuenta_contable
Ref: poliza_detalle.id_centro_costo > centros_costo.id_centro_costo
Ref: poliza_detalle.id_factura > facturas.id_factura
Ref: poliza_detalle.id_factura_proveedor > facturas_proveedor.id_factura_proveedor
Ref: poliza_detalle.id_pago > pagos.id_pago
Ref: poliza_detalle.id_cobro > cobros.id_cobro
Ref: poliza_detalle.id_nomina > nominas.id_nomina
Ref: poliza_detalle.id_movimiento_bancario > movimientos_bancarios.id_movimiento_bancario

Ref: bancos.id_empresa > empresas.id_empresa
Ref: cuentas_bancarias.id_banco > bancos.id_banco
Ref: cuentas_bancarias.id_moneda > monedas.id_moneda

Ref: movimientos_bancarios.id_cuenta_bancaria > cuentas_bancarias.id_cuenta_bancaria
Ref: movimientos_bancarios.id_pago > pagos.id_pago
Ref: movimientos_bancarios.id_cobro > cobros.id_cobro

Ref: conciliaciones_bancarias.id_cuenta_bancaria > cuentas_bancarias.id_cuenta_bancaria
Ref: conciliacion_detalle.id_conciliacion > conciliaciones_bancarias.id_conciliacion
Ref: conciliacion_detalle.id_movimiento_bancario > movimientos_bancarios.id_movimiento_bancario

// AR
Ref: facturas.id_empresa > empresas.id_empresa
Ref: facturas.id_sucursal > sucursales.id_sucursal
Ref: facturas.id_cliente > clientes.id_cliente
Ref: facturas.id_pedido > pedidos.id_pedido
Ref: facturas.id_moneda > monedas.id_moneda

Ref: factura_detalle.id_factura > facturas.id_factura
Ref: factura_detalle.id_pedido_detalle > pedido_detalle.id_pedido_detalle
Ref: factura_detalle.id_producto > productos.id_producto

Ref: cuentas_por_cobrar.id_cliente > clientes.id_cliente
Ref: cuentas_por_cobrar.id_factura > facturas.id_factura

Ref: cobros.id_cliente > clientes.id_cliente
Ref: cobros.id_cuenta_bancaria > cuentas_bancarias.id_cuenta_bancaria
Ref: cobro_detalle.id_cobro > cobros.id_cobro
Ref: cobro_detalle.id_cxc > cuentas_por_cobrar.id_cxc

Ref: notas_credito.id_factura > facturas.id_factura
Ref: nota_credito_detalle.id_nota_credito > notas_credito.id_nota_credito
Ref: nota_credito_detalle.id_factura_detalle > factura_detalle.id_factura_detalle

// AP
Ref: facturas_proveedor.id_empresa > empresas.id_empresa
Ref: facturas_proveedor.id_sucursal > sucursales.id_sucursal
Ref: facturas_proveedor.id_proveedor > proveedores.id_proveedor
Ref: facturas_proveedor.id_oc > ordenes_compra.id_oc
Ref: facturas_proveedor.id_recepcion > recepciones.id_recepcion
Ref: facturas_proveedor.id_moneda > monedas.id_moneda

Ref: factura_proveedor_detalle.id_factura_proveedor > facturas_proveedor.id_factura_proveedor
Ref: factura_proveedor_detalle.id_oc_detalle > orden_compra_detalle.id_oc_detalle
Ref: factura_proveedor_detalle.id_recepcion_detalle > recepcion_detalle.id_recepcion_detalle
Ref: factura_proveedor_detalle.id_producto > productos.id_producto

Ref: cuentas_por_pagar.id_proveedor > proveedores.id_proveedor
Ref: cuentas_por_pagar.id_factura_proveedor > facturas_proveedor.id_factura_proveedor

Ref: pagos.id_proveedor > proveedores.id_proveedor
Ref: pagos.id_cuenta_bancaria > cuentas_bancarias.id_cuenta_bancaria
Ref: pago_detalle.id_pago > pagos.id_pago
Ref: pago_detalle.id_cxp > cuentas_por_pagar.id_cxp

// Proyectos
Ref: proyectos.id_empresa > empresas.id_empresa
Ref: proyectos.id_cliente > clientes.id_cliente
Ref: proyecto_tareas.id_proyecto > proyectos.id_proyecto
Ref: proyecto_asignaciones.id_proyecto_tarea > proyecto_tareas.id_proyecto_tarea
Ref: proyecto_asignaciones.id_empleado > empleados.id_empleado
Ref: proyecto_horas.id_proyecto_tarea > proyecto_tareas.id_proyecto_tarea
Ref: proyecto_horas.id_empleado > empleados.id_empleado
Ref: proyecto_costos.id_proyecto > proyectos.id_proyecto
Ref: proyecto_costos.id_centro_costo > centros_costo.id_centro_costo
Ref: proyecto_facturacion.id_proyecto > proyectos.id_proyecto
Ref: proyecto_facturacion.id_factura > facturas.id_factura

// Ecommerce
Ref: canales_venta.id_empresa > empresas.id_empresa
Ref: tiendas.id_empresa > empresas.id_empresa
Ref: tiendas.id_canal_venta > canales_venta.id_canal_venta
Ref: catalogo_web.id_tienda > tiendas.id_tienda
Ref: catalogo_web_detalle.id_catalogo_web > catalogo_web.id_catalogo_web
Ref: catalogo_web_detalle.id_producto > productos.id_producto
Ref: carritos.id_tienda > tiendas.id_tienda
Ref: carritos.id_cliente > clientes.id_cliente
Ref: carrito_detalle.id_carrito > carritos.id_carrito
Ref: carrito_detalle.id_producto > productos.id_producto
Ref: pagos_online.id_tienda > tiendas.id_tienda
Ref: pagos_online.id_pedido > pedidos.id_pedido
Ref: transacciones_pago.id_pago_online > pagos_online.id_pago_online

// Marketing
Ref: segmentos.id_empresa > empresas.id_empresa
Ref: segmento_clientes.id_segmento > segmentos.id_segmento
Ref: segmento_clientes.id_cliente > clientes.id_cliente
Ref: campanas.id_empresa > empresas.id_empresa
Ref: campana_detalle.id_campana > campanas.id_campana
Ref: campana_detalle.id_segmento > segmentos.id_segmento
Ref: leads.id_empresa > empresas.id_empresa
Ref: leads.id_prospecto > prospectos.id_prospecto
Ref: lead_origen.id_lead > leads.id_lead
Ref: interacciones.id_empresa > empresas.id_empresa
Ref: interacciones.id_cliente > clientes.id_cliente
Ref: interacciones.id_lead > leads.id_lead
Ref: interacciones.id_campana > campanas.id_campana
Ref: interaccion_detalle.id_interaccion > interacciones.id_interaccion
Ref: automatizaciones.id_empresa > empresas.id_empresa
Ref: automatizacion_pasos.id_automatizacion > automatizaciones.id_automatizacion
Ref: automatizacion_pasos.id_campana > campanas.id_campana
Ref: automatizacion_pasos.id_interaccion > interacciones.id_interaccion

// HR + WFM
Ref: puestos.id_empresa > empresas.id_empresa
Ref: empleados.id_empresa > empresas.id_empresa
Ref: empleados.id_sucursal > sucursales.id_sucursal
Ref: empleados.id_departamento > departamentos.id_departamento
Ref: empleados.id_puesto > puestos.id_puesto

Ref: contratos.id_empleado > empleados.id_empleado
Ref: turnos.id_empresa > empresas.id_empresa
Ref: calendarios.id_turno > turnos.id_turno
Ref: asistencias.id_empleado > empleados.id_empleado
Ref: asistencias.id_turno > turnos.id_turno
Ref: control_horas.id_empleado > empleados.id_empleado
Ref: control_horas.id_asistencia > asistencias.id_asistencia
Ref: control_horas.id_proyecto > proyectos.id_proyecto
Ref: control_horas.id_op > ordenes_produccion.id_op

Ref: vacaciones.id_empleado > empleados.id_empleado
Ref: permisos_ausencias.id_empleado > empleados.id_empleado
Ref: incidencias.id_empleado > empleados.id_empleado
Ref: evaluaciones.id_empleado > empleados.id_empleado
Ref: capacitaciones.id_empleado > empleados.id_empleado

Ref: nominas.id_empresa > empresas.id_empresa
Ref: nominas.id_sucursal > sucursales.id_sucursal
Ref: nomina_detalle.id_nomina > nominas.id_nomina
Ref: nomina_detalle.id_empleado > empleados.id_empleado

Ref: productividad.id_empresa > empresas.id_empresa
Ref: productividad.id_departamento > departamentos.id_departamento
Ref: productividad_detalle.id_productividad > productividad.id_productividad
Ref: productividad_detalle.id_empleado > empleados.id_empleado
Ref: productividad_detalle.id_op > ordenes_produccion.id_op
Ref: productividad_detalle.id_proyecto > proyectos.id_proyecto

// Conexión de pedidos con ecommerce/canales/tienda
Ref: pedidos.id_canal_venta > canales_venta.id_canal_venta
Ref: pedidos.id_tienda > tiendas.id_tienda

// Producción -> entrada PT -> ubicación/almacén
Ref: producto_terminado_entradas.id_op > ordenes_produccion.id_op
Ref: producto_terminado_entradas.id_almacen > almacenes.id_almacen
Ref: producto_terminado_entradas.id_ubicacion > ubicaciones.id_ubicacion

// Movimientos inventario también se alimenta de compras (recepción)
Ref: movimientos_inventario.id_recepcion > recepciones.id_recepcion

// WMS -> despacho puede apuntar a envío
Ref: despachos.id_envio > envios.id_envio
