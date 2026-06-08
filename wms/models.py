from django.db import models

class Picking(models.Model):
    pedido = models.ForeignKey('ventas.Pedido', on_delete=models.CASCADE, related_name='pickings')

    class Meta:
        db_table = 'picking'
        verbose_name = 'Picking'
        verbose_name_plural = 'Pickings'
    
    def __str__(self):
        return str(self.id)

class PickingDetalle(models.Model):
    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name='picking_detalle')
    pedido_detalle = models.ForeignKey('ventas.PedidoDetalle', on_delete=models.CASCADE, related_name='picking_detalle')

    class Meta:
        db_table = 'picking_detalle'
        verbose_name = 'Picking Detalle'
        verbose_name_plural = 'Picking Detalles'
    
    def __str__(self):
        return str(self.id)

class Packing(models.Model):
    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name='packings')

    class Meta:
        db_table = 'packing'
        verbose_name = 'Packing'
        verbose_name_plural = 'Packings'
    
    def __str__(self):
        return str(self.id)

class PackingDetalle(models.Model):
    packing = models.ForeignKey(Packing, on_delete=models.CASCADE, related_name='packing_detalle')
    picking_detalle = models.ForeignKey(PickingDetalle, on_delete=models.CASCADE, related_name='packing_detalle')

    class Meta:
        db_table = 'packing_detalle'
        verbose_name = 'Packing Detalle'
        verbose_name_plural = 'Packing Detalles'
    
    def __str__(self):
        return str(self.id)

class Despacho(models.Model):
    packing = models.ForeignKey(Packing, on_delete=models.CASCADE, related_name='despachos')
    envio = models.ForeignKey('logistica.Envio', on_delete=models.CASCADE, related_name='despachos')

    class Meta:
        db_table = 'despachos'
        verbose_name = 'Despacho'
        verbose_name_plural = 'Despachos'
    
    def __str__(self):
        return str(self.id)

class DespachoDetalle(models.Model):
    despacho = models.ForeignKey(Despacho, on_delete=models.CASCADE, related_name='despacho_detalle')
    packing_detalle = models.ForeignKey(PackingDetalle, on_delete=models.CASCADE, related_name='despacho_detalle')

    class Meta:
        db_table = 'despacho_detalle'
        verbose_name = 'Despacho Detalle'
        verbose_name_plural = 'Despacho Detalles'
    
    def __str__(self):
        return str(self.id)

class ConteoCiclico(models.Model):
    almacen = models.ForeignKey('inventarios.Almacen', on_delete=models.CASCADE, related_name='conteo_ciclico')

    class Meta:
        db_table = 'conteos_ciclico'
        verbose_name = 'Conteo Ciclico'
        verbose_name_plural = 'Conteos Cíclicos'
    
    def __str__(self):
        return str(self.id)

class ConteoCiclicoDetalle(models.Model):
    conteo_ciclico = models.ForeignKey(ConteoCiclico, on_delete=models.CASCADE, related_name='conteo_ciclico_detalle')
    existencia = models.ForeignKey('inventarios.Existencia', on_delete=models.CASCADE, related_name='conteo_ciclico_detalle')

    class Meta:
        db_table = 'conteo_ciclico_detalle'
        verbose_name = 'Conteo Ciclico Detalle'
        verbose_name_plural = 'Conteos Cíclicos Detalles'
    
    def __str__(self):
        return str(self.id)

class Transferencia(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name='transferencias')
    sucursal= models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name='transferencias')

    class Meta:
        db_table = 'transferencias'
        verbose_name = 'Transferencia'
        verbose_name_plural = 'Transferencias'
    
    def __str__(self):
        return str(self.id)

class TransferenciaDetalle(models.Model):
    transferencia = models.ForeignKey(Transferencia, on_delete=models.CASCADE, related_name='transferencia_detalle')
    producto = models.ForeignKey('catalogo.Producto', on_delete=models.CASCADE, related_name='transferencia_detalle')
    ubicacion_origen = models.ForeignKey('inventarios.Ubicacion', on_delete=models.CASCADE, related_name='transferencia_detalle_origen')
    ubicacion_destino = models.ForeignKey('inventarios.Ubicacion', on_delete=models.CASCADE, related_name='transferencia_detalle_destino')
    lote = models.ForeignKey('inventarios.Lote', on_delete=models.CASCADE, related_name='transferencia_detalle')
    serie = models.ForeignKey('inventarios.Serie', on_delete=models.CASCADE, related_name='transferencia_detalle')

    class Meta:
        db_table = 'transferencia_detalle'
        verbose_name = 'Transferencia Detalle'
        verbose_name_plural = 'Transferencias Detalles'
    
    def __str__(self):
        return str(self.id)