from django.shortcuts import render, redirect
from django.db import transaction
import uuid
import json
from django.contrib import messages

from produccion.models import OrdenProduccion, OrdenProduccionDetalle, UnidadMedida, ProductoVariante, ListaMaterialBom
from nucleo.models import Empresa, Sucursal, UnidadMedida

# Create your views here.
def index(request):
    return render(request, 'QA/index_QA.html')

# PRODUCCION
def produccion_workspace(request):
    return render(request, 'QA/produccion/produccion_workspace.html')
    
def generar_orden_produccion(request):
    if request.method == 'POST':
        sucursal_id = request.POST.get('sucursal_id')
        prioridad = request.POST.get('prioridad', 1)
        observaciones = request.POST.get('observaciones', '')
        
        # Ahora recibimos variantes en lugar de BOMs directamente
        variante_ids = request.POST.getlist('variante_ids')
        cantidades = request.POST.getlist('cantidades')

        cantidades_validas = [c for c in cantidades if c and float(c) > 0]
        if not cantidades_validas:
            messages.error(request, "Debes ingresar la cantidad de al menos un producto.")
            return redirect('generar_orden_produccion')

        try:
            with transaction.atomic():
                empresa_default = Empresa.objects.first() 
                sucursal = Sucursal.objects.get(id=sucursal_id)
                unidad_default = UnidadMedida.objects.first()
                
                nueva_op = OrdenProduccion.objects.create(
                    empresa=empresa_default,
                    sucursal=sucursal,
                    folio_op=f"OP-{uuid.uuid4().hex[:8].upper()}",
                    prioridad=prioridad,
                    observaciones=observaciones,
                )

                for variante_id, cantidad in zip(variante_ids, cantidades):
                    if cantidad and float(cantidad) > 0:
                        variante = ProductoVariante.objects.get(id=variante_id)
                        # Buscamos el BOM asociado a esta variante
                        bom_instance = ListaMaterialBom.objects.filter(producto_variante=variante, activo=True).first()
                        
                        if bom_instance:
                            OrdenProduccionDetalle.objects.create(
                                op=nueva_op,
                                bom=bom_instance,
                                producto_variante=variante, # Guardamos la variante seleccionada
                                cantidad=float(cantidad),
                                unidad=unidad_default,
                            )

            messages.success(request, f"La orden {nueva_op.folio_op} se generó exitosamente.")
            return redirect('generar_orden_produccion')

        except Exception as e:
            messages.error(request, f"Ocurrió un error al generar la orden: {str(e)}")
            return redirect('generar_orden_produccion')

    # LÓGICA GET: Preparar datos para el frontend
    variantes = ProductoVariante.objects.filter(activo=True)
    
    # Construimos un diccionario con los insumos para que JS pueda multiplicar
    recetas_dict = {}
    for variante in variantes:
        bom = ListaMaterialBom.objects.filter(producto_variante=variante, activo=True).first()
        if bom:
            # IMPORTANTE: Aquí debes iterar sobre los detalles reales de tu BOM. 
            # Esto es un ejemplo estructurado de lo que JS espera recibir:
            recetas_dict[str(variante.id)] = [
                {"insumo": "Botones", "cantidad_unitaria": 3, "unidad": "pzas"},
                {"insumo": "Tela algodón", "cantidad_unitaria": 1.5, "unidad": "mts"}
            ]

    context = {
        'sucursales': Sucursal.objects.all(),
        'variantes': variantes,
        'recetas_json': json.dumps(recetas_dict) # Pasamos el dict como string JSON
    }
    
    return render(request, 'QA/produccion/generar_orden_produccion.html', context)