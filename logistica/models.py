from django.db import models

class Envio(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name='envios')
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name='envios')
    pedido = models.ForeignKey('ventas.Pedido', on_delete=models.CASCADE, related_name='envios')
    transportista = models.ForeignKey('terceros.Transportista', on_delete=models.CASCADE, related_name='envios')

    class Meta:
        db_table = 'envios'
        verbose_name = 'Envio'
        verbose_name_plural = 'Envios'
    
    def __str__(self):
        return str(self.id)

class EnvioDetalle(models.Model):
    envio = models.ForeignKey(Envio, on_delete=models.CASCADE, related_name='envio_detalle')
    entrega = models.ForeignKey('ventas.Entrega', on_delete=models.CASCADE, related_name='envio_detalle')

    class Meta:
        db_table = 'envio_detalle'
        verbose_name = 'Envio Detalle'
        verbose_name_plural = 'Envios Detalles'
    
    def __str__(self):
        return str(self.id)
