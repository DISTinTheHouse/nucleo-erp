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
        if not hasattr(form, 'instance'):
            return super().form_valid(form)

        is_creation = not form.instance.pk

        changes = {}
        if not is_creation and hasattr(self, '_initial_state'):
            current_state = form.cleaned_data
            for field, value in current_state.items():
                old_value = self._initial_state.get(field)
                if old_value != value and old_value is not None:
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
        from auditoria.models import AuditoriaEvento
        
        user = self.request.user
        if not user.is_authenticated:
            return

        # Intentar obtener la empresa del objeto o del usuario
        empresa = getattr(obj, 'empresa', None)
        # Si el objeto no tiene empresa (ej. Empresa misma), usar la del usuario si existe
        if not empresa and hasattr(user, 'empresa_actual'):
             empresa = user.empresa_actual
             
        # Si aun no hay empresa y el objeto ES una empresa, usarse a si misma (caso borde)
        if not empresa and obj._meta.model_name == 'empresa':
            empresa = obj

        if not empresa:
            # Si no hay contexto de empresa, no podemos guardar en AuditoriaEvento (requiere FK)
            # Fallback a log normal o ignorar
            logger.warning(f"No se pudo auditar {action} en {obj} - Falta Empresa")
            return

        ip = self.request.META.get('REMOTE_ADDR')
        user_agent = self.request.META.get('HTTP_USER_AGENT', '')[:200] # Truncar si es necesario

        antes = None
        despues = None

        if changes:
             antes = {k: v['from'] for k, v in changes.items()}
             despues = {k: v['to'] for k, v in changes.items()}
        
        try:
            AuditoriaEvento.objects.create(
                empresa=empresa,
                usuario=user,
                modulo=obj._meta.app_label, # o verbose_name
                accion=action,
                tabla=obj._meta.db_table,
                id_registro=str(obj.pk),
                antes_json=antes,
                despues_json=despues,
                ip=ip,
                user_agent=user_agent
            )
        except Exception as e:
            logger.error(f"Error guardando auditoria DB: {e}")
