import logging
import json

logger = logging.getLogger('audit_logger')

class AuditLogMixin:
    """
    Mixin para registrar acciones de creación, edición y eliminación en los logs de auditoría.
    Ahora soporta detección de cambios en campos específicos (UPDATE).
    """
    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Guardamos el estado inicial para comparar después
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            self._initial_state = self._get_model_state(obj)
        return obj

    def form_valid(self, form):
        # Determinamos si es creación o actualización
        is_creation = not form.instance.pk
        
        # Si es actualización, calculamos cambios antes de guardar (ya tenemos _initial_state del get_object)
        # Nota: form_valid guarda el objeto, así que comparamos form.cleaned_data con _initial_state
        changes = {}
        if not is_creation and hasattr(self, '_initial_state'):
            current_state = form.cleaned_data
            for field, value in current_state.items():
                # Ignoramos campos M2M en esta comparación simple o manejamos tipos complejos
                old_value = self._initial_state.get(field)
                if old_value != value and old_value is not None: # Solo registramos si había un valor previo
                    changes[field] = {'from': str(old_value), 'to': str(value)}

        response = super().form_valid(form)
        
        action = 'CREATE' if is_creation else 'UPDATE'
        self.log_audit_action(form.instance, action, changes if action == 'UPDATE' else None)
        return response

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        response = super().delete(request, *args, **kwargs)
        self.log_audit_action(self.object, 'DELETE')
        return response

    def _get_model_state(self, obj):
        # Captura el estado actual de los campos del modelo
        state = {}
        for field in obj._meta.fields:
            try:
                state[field.name] = getattr(obj, field.name)
            except:
                pass
        return state

    def log_audit_action(self, obj, action, changes=None):
        user = self.request.user
        model_name = obj._meta.verbose_name
        object_str = str(obj)
        
        extra_info = ""
        if changes:
            # Formateamos los cambios para que sean legibles en una línea
            change_list = [f"{k}: {v['from']} -> {v['to']}" for k, v in changes.items()]
            extra_info = f" | Cambios: [{', '.join(change_list)}]"
            
        msg = f"Usuario: {user.email} | Accion: {action} | Modelo: {model_name} | Objeto: '{object_str}' (ID: {obj.pk}){extra_info}"
        logger.info(msg)
