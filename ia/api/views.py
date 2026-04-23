from django.conf import settings
from django.core import signing
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.text import slugify
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
import json
import secrets
import urllib.error
import urllib.request

from nucleo.models import Empresa
from nucleo.api.serializers import EmpresaSerializer
from seguridad.models import Rol
from terceros.api.serializers import ClienteSerializer
from usuarios.api.serializers import UsuarioSerializer
from usuarios.models import Usuario
from ventas.models import Cotizacion, Pedido
from terceros.models import Cliente
from django.db.models import Q
from django.utils import timezone
from datetime import date
from datetime import timedelta
from email.message import EmailMessage
import base64
import urllib.parse
from ia.models import CloudIntegration
from ia.views import GOOGLE_DRIVE_SCOPE, _google_drive_credentials_configured, _google_drive_refresh_token, _http_json


class AIAssistantAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
        base_url = (getattr(settings, "OPENAI_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")
        model = getattr(settings, "OPENAI_MODEL", "") or "gpt-4o-mini"

        if not api_key:
            return Response(
                {"error": "OPENAI_API_KEY no está configurado en el servidor."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user_message = (request.data.get("message") or "").strip()
        if not user_message:
            return Response({"error": "message es requerido."}, status=status.HTTP_400_BAD_REQUEST)

        incoming_conversation = request.data.get("conversation")
        conversation = []
        if isinstance(incoming_conversation, list):
            for m in incoming_conversation[-20:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    conversation.append({"role": role, "content": content.strip()})

        now_str = timezone.now().strftime("%Y-%m-%d %H:%M")
        system_prompt = (
            f"Eres un asistente dentro de un ERP. Responde en español. Fecha y hora actual: {now_str}. "
            "Si necesitas datos del sistema (cotizaciones, clientes, pedidos, empresas, usuarios) usa herramientas. "
            "Nunca inventes números. "
            "Para crear recursos del sistema ERP, pide los campos requeridos y valida permisos: "
            "solo superuser puede crear empresas/roles; admin de empresa puede crear usuarios; "
            "usuarios normales no pueden crear recursos en el ERP. "
            "SIN EMBARGO, para el calendario y correos, TODOS los usuarios pueden crear eventos y enviar correos, asume que siempre tienen permiso."
        )

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation)
        messages.append({"role": "user", "content": user_message})

        tools = self._tools_schema()
        tool_results = []

        for _ in range(3):
            ai = self._openai_chat(base_url=base_url, api_key=api_key, model=model, messages=messages, tools=tools)
            if "error" in ai:
                return Response(ai, status=status.HTTP_502_BAD_GATEWAY)

            choice = (ai.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return Response(
                    {
                        "reply": (msg.get("content") or "").strip(),
                        "tool_results": tool_results,
                    }
                )

            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}

                result = self._execute_tool(request=request, name=name, args=args)
                tool_results.append({"name": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        return Response(
            {
                "reply": "No pude completar la solicitud en este momento. Intenta reformular la petición.",
                "tool_results": tool_results,
            },
            status=status.HTTP_200_OK,
        )

    def _openai_chat(self, base_url, api_key, model, messages, tools):
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = ""
            return {"error": "OpenAI HTTPError", "status_code": e.code, "body": body}
        except Exception as e:
            return {"error": "OpenAI request failed", "detail": str(e)}

    def _tools_schema(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_counts",
                    "description": "Obtiene conteos (según el alcance/permisos del usuario) de empresas, usuarios y cotizaciones.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_cotizaciones_summary",
                    "description": "Obtiene conteos de cotizaciones totales y autorizadas (aprobadas) para el usuario/empresa. Permite filtrar por periodo.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "period": {
                                "type": "string",
                                "description": "Opcional. hoy | este_mes | this_month | all",
                            },
                            "date_from": {"type": "string", "description": "Opcional. Fecha ISO YYYY-MM-DD"},
                            "date_to": {"type": "string", "description": "Opcional. Fecha ISO YYYY-MM-DD"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_clientes_count",
                    "description": "Obtiene el conteo de clientes activos según permisos (empresa completa o solo los asignados al vendedor).",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_last_cliente_compra",
                    "description": "Obtiene el último cliente que compró (último pedido autorizado/en proceso) según permisos. Permite filtrar por periodo.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "period": {
                                "type": "string",
                                "description": "Opcional. hoy | este_mes | this_month | all",
                            },
                            "date_from": {"type": "string", "description": "Opcional. Fecha ISO YYYY-MM-DD"},
                            "date_to": {"type": "string", "description": "Opcional. Fecha ISO YYYY-MM-DD"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_empresa",
                    "description": "Crea una empresa. Solo superuser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "codigo": {"type": "string"},
                            "razon_social": {"type": "string"},
                            "nombre_comercial": {"type": "string"},
                            "rfc": {"type": "string"},
                            "email_contacto": {"type": "string"},
                            "telefono": {"type": "string"},
                            "sitio_web": {"type": "string"},
                            "moneda_base": {"type": "string"},
                            "timezone": {"type": "string"},
                            "idioma": {"type": "string"},
                            "logo_url": {"type": "string"},
                        },
                        "required": ["codigo", "razon_social"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_rol",
                    "description": "Crea un rol para una empresa. Solo superuser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "empresa_id": {"type": "integer"},
                            "nombre": {"type": "string"},
                            "descripcion": {"type": "string"},
                            "clave_departamento": {"type": "string"},
                            "estatus": {"type": "string"},
                        },
                        "required": ["empresa_id", "nombre"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_usuario",
                    "description": "Crea un usuario. Superuser o admin de empresa (admin crea solo en su empresa).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "username": {"type": "string"},
                            "email": {"type": "string"},
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "telefono": {"type": "string"},
                            "avatar_url": {"type": "string"},
                            "empresa": {"type": "integer"},
                            "sucursal_default": {"type": "integer"},
                            "sucursales": {"type": "array", "items": {"type": "integer"}},
                            "departamentos": {"type": "array", "items": {"type": "integer"}},
                            "is_admin_empresa": {"type": "boolean"},
                            "password": {"type": "string"},
                            "roles": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["username", "email", "empresa", "sucursal_default"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_cliente",
                    "description": "Crea un cliente y lo asigna al vendedor actual. Campos opcionales para cotizaciones rápidas.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "razon_social": {"type": "string"},
                            "nombre": {"type": "string"},
                            "telefono": {"type": "string"},
                            "correo": {"type": "string"},
                            "rfc": {"type": "string"},
                            "sat_regimen_fiscal": {"type": "integer"},
                            "direccion_fiscal": {"type": "string"},
                            "colonia": {"type": "string"},
                            "codigo_postal": {"type": "string"},
                            "ciudad": {"type": "string"},
                            "estado": {"type": "string"},
                            "giro_empresarial": {"type": "string"},
                            "sat_uso_cfdi": {"type": "integer"},
                        },
                        "required": [
                            "nombre",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_calendar_event",
                    "description": "Crea un evento en el calendario del usuario. Solo si el usuario tiene Google Calendar conectado.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "Título del evento"},
                            "description": {"type": "string", "description": "Descripción del evento (opcional)"},
                            "start_date": {"type": "string", "description": "Fecha de inicio YYYY-MM-DD"},
                            "start_time": {"type": "string", "description": "Hora de inicio HH:MM (opcional para eventos de todo el día)"},
                            "end_date": {"type": "string", "description": "Fecha de fin YYYY-MM-DD"},
                            "end_time": {"type": "string", "description": "Hora de fin HH:MM (opcional para eventos de todo el día)"},
                        },
                        "required": ["summary", "start_date"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_email",
                    "description": "Envía un correo electrónico desde la cuenta del usuario. Solo si el usuario tiene Gmail conectado.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string", "description": "Dirección de correo del destinatario"},
                            "subject": {"type": "string", "description": "Asunto del correo"},
                            "body": {"type": "string", "description": "Cuerpo del correo en texto plano"},
                        },
                        "required": ["to", "subject", "body"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_pending_summary",
                    "description": "Obtiene un resumen de próximos eventos en el calendario y los correos recientes recibidos en la bandeja de entrada.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _execute_tool(self, request, name, args):
        if name == "get_counts":
            return self._tool_get_counts(request)
        if name == "get_cotizaciones_summary":
            return self._tool_get_cotizaciones_summary(request, args)
        if name == "get_clientes_count":
            return self._tool_get_clientes_count(request)
        if name == "get_last_cliente_compra":
            return self._tool_get_last_cliente_compra(request, args)
        if name == "create_empresa":
            return self._tool_create_empresa(request, args)
        if name == "create_rol":
            return self._tool_create_rol(request, args)
        if name == "create_usuario":
            return self._tool_create_usuario(request, args)
        if name == "create_cliente":
            return self._tool_create_cliente(request, args)
        if name == "create_calendar_event":
            return self._tool_create_calendar_event(request, args)
        if name == "send_email":
            return self._tool_send_email(request, args)
        if name == "get_pending_summary":
            return self._tool_get_pending_summary(request, args)
        return {"ok": False, "error": f"Herramienta desconocida: {name}"}

    def _parse_date_filters(self, args):
        period = (args or {}).get("period")
        date_from_raw = (args or {}).get("date_from")
        date_to_raw = (args or {}).get("date_to")
        if isinstance(date_from_raw, str) and date_from_raw.strip():
            try:
                d_from = date.fromisoformat(date_from_raw.strip())
            except ValueError:
                d_from = None
        else:
            d_from = None
        if isinstance(date_to_raw, str) and date_to_raw.strip():
            try:
                d_to = date.fromisoformat(date_to_raw.strip())
            except ValueError:
                d_to = None
        else:
            d_to = None
        if d_from or d_to:
            return d_from, d_to
        period = (period or "all").strip().lower()
        today = timezone.localdate()
        if period in ("hoy", "today"):
            return today, today
        if period in ("este_mes", "this_month", "mes_actual"):
            first_day = today.replace(day=1)
            return first_day, today
        return None, None

    def _tool_get_counts(self, request):
        user = request.user

        if getattr(user, "is_superuser", False):
            empresas_qs = Empresa.objects.filter(activo=True)
        else:
            empresa_ids = []
            if getattr(user, "empresa_id", None):
                empresa_ids.append(user.empresa_id)
            try:
                empresa_ids += list(user.empresas.values_list("pk", flat=True))
            except Exception:
                pass
            empresas_qs = Empresa.objects.filter(activo=True, pk__in=set(empresa_ids))

        if getattr(user, "is_superuser", False):
            usuarios_qs = Usuario.objects.all()
        else:
            empresa = getattr(user, "empresa", None)
            usuarios_qs = Usuario.objects.filter(empresa=empresa) if empresa else Usuario.objects.none()

        cot_qs = Cotizacion.objects.all()
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cot_qs = cot_qs.filter(empresa=empresa)
                if not getattr(user, "is_admin_empresa", False):
                    cot_qs = cot_qs.filter(vendedor=user)
            else:
                cot_qs = cot_qs.none()

        return {
            "ok": True,
            "scope": {
                "is_superuser": bool(getattr(user, "is_superuser", False)),
                "is_admin_empresa": bool(getattr(user, "is_admin_empresa", False)),
                "empresa_id": getattr(user, "empresa_id", None),
            },
            "counts": {
                "empresas": empresas_qs.count(),
                "usuarios": usuarios_qs.count(),
                "cotizaciones": cot_qs.count(),
            },
        }

    def _tool_get_cotizaciones_summary(self, request, args):
        user = request.user
        cot_qs = Cotizacion.objects.all()
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cot_qs = cot_qs.filter(empresa=empresa)
                if not getattr(user, "is_admin_empresa", False):
                    cot_qs = cot_qs.filter(vendedor=user)
            else:
                cot_qs = cot_qs.none()

        d_from, d_to = self._parse_date_filters(args)
        if d_from and d_to:
            cot_qs = cot_qs.filter(created_at__date__gte=d_from, created_at__date__lte=d_to)
        elif d_from:
            cot_qs = cot_qs.filter(created_at__date__gte=d_from)
        elif d_to:
            cot_qs = cot_qs.filter(created_at__date__lte=d_to)

        total = cot_qs.count()
        aprobadas = cot_qs.filter(estatus=3).count()
        return {
            "ok": True,
            "filters": {"date_from": d_from.isoformat() if d_from else None, "date_to": d_to.isoformat() if d_to else None},
            "counts": {"cotizaciones": total, "cotizaciones_aprobadas": aprobadas},
        }

    def _tool_get_clientes_count(self, request):
        user = request.user
        qs = Cliente.objects.filter(activo=True)
        if getattr(user, "is_superuser", False):
            return {"ok": True, "counts": {"clientes": qs.count()}}
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return {"ok": True, "counts": {"clientes": 0}}
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return {"ok": True, "counts": {"clientes": qs.count()}}
        qs = qs.filter(vendedores__id=getattr(user, "id", None))
        return {"ok": True, "counts": {"clientes": qs.count()}}

    def _tool_get_last_cliente_compra(self, request, args):
        user = request.user
        pedidos_qs = Pedido.objects.filter(activo=True).exclude(estatus=5)
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if empresa:
                pedidos_qs = pedidos_qs.filter(empresa=empresa)
                if not getattr(user, "is_admin_empresa", False):
                    pedidos_qs = pedidos_qs.filter(Q(cliente__vendedores=user) | Q(cotizacion__vendedor=user))
            else:
                pedidos_qs = pedidos_qs.none()

        d_from, d_to = self._parse_date_filters(args)
        if d_from and d_to:
            pedidos_qs = pedidos_qs.filter(created_at__date__gte=d_from, created_at__date__lte=d_to)
        elif d_from:
            pedidos_qs = pedidos_qs.filter(created_at__date__gte=d_from)
        elif d_to:
            pedidos_qs = pedidos_qs.filter(created_at__date__lte=d_to)

        pedido = pedidos_qs.order_by("-created_at", "-id").select_related("cliente").first()
        if not pedido:
            return {
                "ok": True,
                "filters": {"date_from": d_from.isoformat() if d_from else None, "date_to": d_to.isoformat() if d_to else None},
                "last_purchase": None,
            }
        cliente = getattr(pedido, "cliente", None)
        return {
            "ok": True,
            "filters": {"date_from": d_from.isoformat() if d_from else None, "date_to": d_to.isoformat() if d_to else None},
            "last_purchase": {
                "pedido_id": pedido.pk,
                "folio": getattr(pedido, "folio", None),
                "fecha": pedido.created_at.isoformat() if getattr(pedido, "created_at", None) else None,
                "gran_total": str(getattr(pedido, "gran_total", "")),
                "cliente": {
                    "id": getattr(cliente, "pk", None),
                    "razon_social": getattr(cliente, "razon_social", None),
                    "nombre": getattr(cliente, "nombre", None),
                    "rfc": getattr(cliente, "rfc", None),
                },
            },
        }

    def _tool_create_empresa(self, request, args):
        user = request.user
        if not getattr(user, "is_superuser", False):
            return {"ok": False, "error": "No autorizado. Solo superuser puede crear empresas."}

        serializer = EmpresaSerializer(data=args, context={"request": request})
        if not serializer.is_valid():
            return {"ok": False, "error": "Validación falló", "details": serializer.errors}

        empresa = serializer.save()

        if not getattr(user, "empresa_id", None):
            user.empresa = empresa
            user.is_admin_empresa = True
            user.save(update_fields=["empresa", "is_admin_empresa"])
        try:
            user.empresas.add(empresa)
        except Exception:
            pass

        return {"ok": True, "empresa": {"id": empresa.pk, "codigo": empresa.codigo, "razon_social": empresa.razon_social}}

    def _tool_create_rol(self, request, args):
        user = request.user
        if not getattr(user, "is_superuser", False):
            return {"ok": False, "error": "No autorizado. Solo superuser puede crear roles."}

        empresa_id = args.get("empresa_id")
        nombre = (args.get("nombre") or "").strip()
        if not empresa_id or not nombre:
            return {"ok": False, "error": "empresa_id y nombre son requeridos."}

        try:
            empresa = Empresa.objects.get(pk=empresa_id, activo=True)
        except Empresa.DoesNotExist:
            return {"ok": False, "error": "Empresa no existe o está inactiva."}

        descripcion = args.get("descripcion") or ""
        clave_departamento = args.get("clave_departamento")
        estatus = args.get("estatus") or "activo"

        rol = Rol(
            empresa=empresa,
            codigo="tmp",
            nombre=nombre,
            descripcion=descripcion,
            clave_departamento=clave_departamento,
            estatus=estatus,
        )
        rol.save()
        rol.codigo = f"{slugify(nombre)[:10]}-{rol.pk}"
        rol.save(update_fields=["codigo"])

        return {"ok": True, "rol": {"id": rol.pk, "codigo": rol.codigo, "nombre": rol.nombre, "empresa_id": empresa.pk}}

    def _tool_create_usuario(self, request, args):
        request_user = request.user
        is_superuser = bool(getattr(request_user, "is_superuser", False))
        is_admin_empresa = bool(getattr(request_user, "is_admin_empresa", False))

        if not (is_superuser or is_admin_empresa):
            return {"ok": False, "error": "No autorizado. Solo superuser o admin de empresa puede crear usuarios."}

        payload = dict(args or {})

        if not is_superuser and is_admin_empresa:
            payload["empresa"] = getattr(request_user, "empresa_id", None)
            payload["is_admin_empresa"] = False

        serializer = UsuarioSerializer(data=payload, context={"request": request})
        if not serializer.is_valid():
            return {"ok": False, "error": "Validación falló", "details": serializer.errors}

        if not is_superuser and is_admin_empresa:
            if serializer.validated_data.get("is_superuser", False):
                return {"ok": False, "error": "No puedes crear superusuarios."}
            if serializer.validated_data.get("is_admin_empresa", False):
                return {"ok": False, "error": "No puedes crear otros administradores de empresa."}
            sucursal = serializer.validated_data.get("sucursal_default")
            if sucursal and getattr(sucursal, "empresa_id", None) != getattr(request_user, "empresa_id", None):
                return {"ok": False, "error": "La sucursal seleccionada no pertenece a tu empresa."}

        created_user = serializer.save()
        return {
            "ok": True,
            "usuario": {
                "id": created_user.pk,
                "username": created_user.username,
                "email": created_user.email,
                "empresa_id": created_user.empresa_id,
            },
        }

    def _tool_create_cliente(self, request, args):
        user = request.user
        empresa = getattr(user, "empresa", None)
        if not getattr(user, "is_superuser", False) and not empresa:
            return {"ok": False, "error": "No tienes empresa activa para crear clientes."}

        payload = dict(args or {})
        serializer = ClienteSerializer(data=payload, context={"request": request})
        if not serializer.is_valid():
            return {"ok": False, "error": "Validación falló", "details": serializer.errors}

        cliente = serializer.save(empresa=empresa)
        try:
            cliente.vendedores.add(user)
        except Exception:
            pass

        return {"ok": True, "cliente": {"id": cliente.pk, "razon_social": cliente.razon_social, "empresa_id": cliente.empresa_id}}

    def _tool_create_calendar_event(self, request, args):
        user = request.user
        integration = CloudIntegration.objects.filter(user=user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
        if not integration or not integration.access_token:
            return {"ok": False, "error": "No tienes conectada la cuenta de Google para acceder a Calendario."}
            
        scopes = integration.metadata.get("scope", "") if integration.metadata else ""
        if "calendar" not in scopes.lower():
            return {"ok": False, "error": "Se requieren permisos de Calendario en tu cuenta de Google. Vuelve a conectarla."}

        try:
            access_token = _google_drive_refresh_token(integration)
        except Exception:
            return {"ok": False, "error": "Tu sesión de Google expiró. Vuelve a conectarte."}

        summary = args.get("summary")
        description = args.get("description", "")
        start_date = args.get("start_date")
        start_time = args.get("start_time")
        end_date = args.get("end_date") or start_date
        end_time = args.get("end_time")

        if not summary or not start_date:
            return {"ok": False, "error": "Faltan campos obligatorios para el evento (summary, start_date)."}

        try:
            import datetime
            # Construct ISO 8601 strings
            if start_time:
                if len(start_time) == 4: start_time = f"0{start_time}"
                start_dt = f"{start_date}T{start_time}:00"
                
                if not end_time:
                    try:
                        h, m = map(int, start_time.split(':'))
                        h = (h + 1) % 24
                        end_time = f"{h:02d}:{m:02d}"
                    except:
                        end_time = start_time
                elif len(end_time) == 4:
                    end_time = f"0{end_time}"
                    
                end_dt = f"{end_date}T{end_time}:00"
                start_data = {"dateTime": start_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
                end_data = {"dateTime": end_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
            else:
                # All day event
                start_data = {"date": start_date}
                try:
                    ed = datetime.datetime.strptime(end_date, "%Y-%m-%d").date() + datetime.timedelta(days=1)
                    end_date_exclusive = ed.isoformat()
                except Exception:
                    end_date_exclusive = end_date
                end_data = {"date": end_date_exclusive}

            event_body = {
                "summary": summary,
                "description": description,
                "start": start_data,
                "end": end_data,
            }

            _http_json(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                method="POST",
                headers={"Authorization": f"Bearer {access_token}"},
                json_data=event_body
            )
            return {"ok": True, "message": "Evento creado correctamente."}
        except Exception as e:
            return {"ok": False, "error": f"Error al crear el evento en el calendario: {str(e)}"}

    def _tool_send_email(self, request, args):
        user = request.user
        integration = CloudIntegration.objects.filter(user=user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
        if not integration or not integration.access_token:
            return {"ok": False, "error": "No tienes conectada la cuenta de Google para acceder a Gmail."}

        try:
            access_token = _google_drive_refresh_token(integration)
        except Exception:
            return {"ok": False, "error": "Tu sesión de Google expiró. Vuelve a conectarte."}

        to_email = args.get("to")
        subject = args.get("subject", "(Sin asunto)")
        body = args.get("body", "")

        if not to_email or not body:
            return {"ok": False, "error": "Faltan campos obligatorios para enviar el correo (to, body)."}

        try:
            message = EmailMessage()
            message.set_content(body)
            message['To'] = to_email
            message['From'] = integration.account_email
            message['Subject'] = subject

            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

            _http_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                method="POST",
                headers={"Authorization": f"Bearer {access_token}"},
                json_data={"raw": encoded_message}
            )
            return {"ok": True, "message": "Correo enviado correctamente."}
        except Exception as e:
            return {"ok": False, "error": f"Error al enviar el correo: {str(e)}"}

    def _tool_get_pending_summary(self, request, args):
        user = request.user
        integration = CloudIntegration.objects.filter(user=user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
        if not integration or not integration.access_token:
            return {"ok": False, "error": "No tienes conectada la cuenta de Google. Conéctala para obtener resumen."}

        try:
            access_token = _google_drive_refresh_token(integration)
        except Exception:
            return {"ok": False, "error": "Tu sesión de Google expiró. Vuelve a conectarte."}

        summary_data = {"events": [], "emails": []}

        # Fetch upcoming events
        try:
            now = timezone.now().isoformat()
            url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin={urllib.parse.quote(now)}&maxResults=5&singleEvents=true&orderBy=startTime"
            response = _http_json(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                }
            )
            for item in response.get("items", []):
                start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
                end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
                summary_data["events"].append({
                    "summary": item.get("summary") or "Sin título",
                    "start": start,
                    "end": end,
                })
        except Exception:
            pass

        # Fetch recent unread emails
        try:
            response = _http_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=5&q=in:inbox is:unread",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            for msg in response.get("messages", []):
                msg_detail = _http_json(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From",
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                headers = {h['name']: h['value'] for h in msg_detail.get('payload', {}).get('headers', [])}
                from_header = headers.get('From', '')
                from_name = from_header.split('<')[0].strip().strip('"\'') if '<' in from_header else from_header
                summary_data["emails"].append({
                    "subject": headers.get('Subject', '(Sin asunto)'),
                    "from": from_name,
                })
        except Exception:
            pass

        return {"ok": True, "summary": summary_data}


class GoogleCalendarEventsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_integration(self, user):
        integration = CloudIntegration.objects.filter(
            user=user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE
        ).first()
        if not integration or not integration.access_token:
            return None, {
                "ok": False,
                "error": "Conecta tu cuenta de Google primero para usar Calendario.",
                "reconnect_required": True,
            }
        scopes = integration.metadata.get("scope", "") if integration.metadata else ""
        if "calendar" not in (scopes or "").lower():
            return None, {
                "ok": False,
                "error": "Se requieren permisos de Calendario. Desconecta y vuelve a conectar tu cuenta de Google.",
                "reconnect_required": True,
            }
        return integration, None

    def get(self, request):
        integration, error = self._get_integration(request.user)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        access_token = _google_drive_refresh_token(integration)

        time_min = (request.query_params.get("timeMin") or "").strip()
        time_max = (request.query_params.get("timeMax") or "").strip()
        max_results_raw = (request.query_params.get("maxResults") or "").strip()
        page_token = (request.query_params.get("pageToken") or "").strip()

        if not time_min:
            time_min = timezone.now().isoformat()

        max_results = 50
        if max_results_raw:
            try:
                max_results = int(max_results_raw)
            except Exception:
                max_results = 50
        max_results = max(1, min(max_results, 250))

        qp = {
            "timeMin": time_min,
            "maxResults": str(max_results),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_max:
            qp["timeMax"] = time_max
        if page_token:
            qp["pageToken"] = page_token

        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode(
            qp, quote_via=urllib.parse.quote
        )

        try:
            response = _http_json(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        except urllib.error.HTTPError as e:
            return Response(
                {"ok": False, "error": "Error al consultar tu calendario. Intenta reconectar tu cuenta de Google."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            return Response(
                {"ok": False, "error": "Error inesperado al cargar eventos."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        events = []
        for item in response.get("items") or []:
            start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
            events.append(
                {
                    "id": item.get("id"),
                    "summary": item.get("summary") or "Sin título",
                    "description": item.get("description") or "",
                    "htmlLink": item.get("htmlLink"),
                    "start": start,
                    "end": end,
                    "status": item.get("status"),
                    "creator": item.get("creator", {}).get("email"),
                    "updated": item.get("updated"),
                    "location": item.get("location") or "",
                }
            )

        return Response(
            {
                "ok": True,
                "events": events,
                "nextPageToken": response.get("nextPageToken"),
            }
        )

    def post(self, request):
        integration, error = self._get_integration(request.user)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        access_token = _google_drive_refresh_token(integration)

        summary = (request.data.get("summary") or "").strip()
        description = request.data.get("description") or ""
        start_date = (request.data.get("start_date") or "").strip()
        start_time = (request.data.get("start_time") or "").strip()
        end_date = (request.data.get("end_date") or "").strip()
        end_time = (request.data.get("end_time") or "").strip()

        if not summary or not start_date:
            return Response(
                {"ok": False, "error": "summary y start_date son requeridos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not end_date:
            end_date = start_date

        try:
            if start_time:
                start_dt = f"{start_date}T{start_time}:00"
                if end_time:
                    end_dt = f"{end_date}T{end_time}:00"
                else:
                    try:
                        hh, mm = [int(x) for x in start_time.split(":")]
                        end_dt = f"{end_date}T{(hh + 1) % 24:02d}:{mm:02d}:00"
                    except Exception:
                        end_dt = f"{end_date}T{start_time}:00"
                start_data = {"dateTime": start_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
                end_data = {"dateTime": end_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
            else:
                end_exclusive = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
                start_data = {"date": start_date}
                end_data = {"date": end_exclusive}
        except Exception:
            return Response(
                {"ok": False, "error": "Fechas u horas inválidas."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        event_body = {
            "summary": summary,
            "description": description,
            "start": start_data,
            "end": end_data,
        }

        try:
            created = _http_json(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                method="POST",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                json_data=event_body,
            )
        except urllib.error.HTTPError:
            return Response(
                {"ok": False, "error": "Error al crear el evento en el calendario."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            return Response(
                {"ok": False, "error": "Error inesperado al crear el evento."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "ok": True,
                "event": {
                    "id": created.get("id"),
                    "htmlLink": created.get("htmlLink"),
                    "status": created.get("status"),
                },
            },
            status=status.HTTP_201_CREATED,
        )


def _is_production_like() -> bool:
    return bool(getattr(settings, "IS_VERCEL", False) or (getattr(settings, "ENVIRONMENT", "") or "").lower() == "production")


def _cookie_samesite() -> str:
    return "None" if _is_production_like() else "Lax"


IA_GOOGLE_OAUTH_STATE_COOKIE = "ia_google_oauth_state"
IA_GOOGLE_OAUTH_USER_COOKIE = "ia_google_oauth_user"
IA_GOOGLE_OAUTH_NEXT_COOKIE = "ia_google_oauth_next"
IA_GOOGLE_OAUTH_STATE_SALT = "ia-google-oauth-state"


def _clean_next_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""

    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1") or host.endswith(".vercel.app") or host.endswith(".onrender.com"):
        return raw

    return ""


class GoogleOAuthConnectAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not _google_drive_credentials_configured():
            return Response(
                {"ok": False, "error": "Falta configurar GOOGLE_DRIVE_CLIENT_ID y GOOGLE_DRIVE_CLIENT_SECRET."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        existing = CloudIntegration.objects.filter(user=request.user).exclude(provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
        if existing:
            return Response(
                {"ok": False, "error": "Ya tienes una nube conectada. Desconéctala antes de elegir otra."},
                status=status.HTTP_409_CONFLICT,
            )

        next_url = _clean_next_url(request.data.get("next") or request.query_params.get("next"))
        state_payload = {
            "uid": getattr(request.user, "id", None),
            "next": next_url,
            "nonce": secrets.token_urlsafe(16),
        }
        state = signing.dumps(state_payload, salt=IA_GOOGLE_OAUTH_STATE_SALT, compress=True)
        redirect_uri = request.build_absolute_uri(reverse("ai_google_oauth_callback"))
        client_id = (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip()

        user_payload = {"uid": getattr(request.user, "id", None)}
        signed_user = signing.dumps(user_payload, salt="ia-google-oauth", compress=True)

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "scope": GOOGLE_DRIVE_SCOPE,
            "state": state,
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

        response = Response(
            {
                "ok": True,
                "provider": CloudIntegration.PROVIDER_GOOGLE_DRIVE,
                "auth_url": auth_url,
                "redirect_uri": redirect_uri,
                "scope": GOOGLE_DRIVE_SCOPE,
            },
            status=status.HTTP_200_OK,
        )

        cookie_kwargs = {
            "max_age": 600,
            "secure": _is_production_like(),
            "httponly": True,
            "samesite": _cookie_samesite(),
            "path": "/",
        }
        response.set_cookie(IA_GOOGLE_OAUTH_STATE_COOKIE, state, **cookie_kwargs)
        response.set_cookie(IA_GOOGLE_OAUTH_USER_COOKIE, signed_user, **cookie_kwargs)
        response.set_cookie(IA_GOOGLE_OAUTH_NEXT_COOKIE, next_url, **cookie_kwargs)
        return response


class GoogleOAuthCallbackAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        qs = request.query_params
        oauth_error = (qs.get("error") or "").strip()
        code = (qs.get("code") or "").strip()
        state = (qs.get("state") or "").strip()

        cookies = getattr(request, "COOKIES", None) or getattr(getattr(request, "_request", None), "COOKIES", {})
        expected_state = (cookies.get(IA_GOOGLE_OAUTH_STATE_COOKIE) or "").strip()
        signed_user = (cookies.get(IA_GOOGLE_OAUTH_USER_COOKIE) or "").strip()
        cookie_next_url = _clean_next_url(cookies.get(IA_GOOGLE_OAUTH_NEXT_COOKIE) or "")

        signed_state_user_id = None
        signed_state_next_url = ""
        if state:
            try:
                signed_state = signing.loads(state, salt=IA_GOOGLE_OAUTH_STATE_SALT, max_age=900)
                signed_state_user_id = signed_state.get("uid")
                signed_state_next_url = _clean_next_url(signed_state.get("next"))
            except Exception:
                signed_state_user_id = None
                signed_state_next_url = ""

        next_url = signed_state_next_url or cookie_next_url

        redirect_uri = request.build_absolute_uri(reverse("ai_google_oauth_callback"))

        def _final_redirect(url: str, params: dict):
            if not url:
                return Response(params, status=status.HTTP_200_OK)
            parsed = urllib.parse.urlparse(url)
            existing_qs = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            merged_qs = {**existing_qs, **params}
            new_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(merged_qs)))
            resp = HttpResponseRedirect(new_url)
            resp.delete_cookie(IA_GOOGLE_OAUTH_STATE_COOKIE, path="/")
            resp.delete_cookie(IA_GOOGLE_OAUTH_USER_COOKIE, path="/")
            resp.delete_cookie(IA_GOOGLE_OAUTH_NEXT_COOKIE, path="/")
            return resp

        if oauth_error:
            return _final_redirect(next_url, {"ok": "0", "error": oauth_error})

        if not code:
            return _final_redirect(next_url, {"ok": "0", "error": "missing_code"})

        if signed_state_user_id is None:
            if not state or not expected_state or state != expected_state:
                return _final_redirect(
                    next_url,
                    {
                        "ok": "0",
                        "error": "invalid_state",
                        "detail": "Inicia el flujo con POST /api/v1/ai/google/oauth/connect/ y luego redirige a auth_url.",
                    },
                )

        if signed_state_user_id is not None:
            user_id = signed_state_user_id
        else:
            try:
                payload = signing.loads(signed_user, salt="ia-google-oauth", max_age=900)
            except Exception:
                return _final_redirect(next_url, {"ok": "0", "error": "invalid_user"})

            user_id = payload.get("uid")
            if not user_id:
                return _final_redirect(next_url, {"ok": "0", "error": "invalid_user"})

        existing = CloudIntegration.objects.filter(user_id=user_id).exclude(provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
        if existing:
            return _final_redirect(next_url, {"ok": "0", "error": "provider_locked"})

        try:
            token_data = _http_json(
                "https://oauth2.googleapis.com/token",
                method="POST",
                data={
                    "client_id": (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip(),
                    "client_secret": (getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "") or "").strip(),
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            access_token = (token_data.get("access_token") or "").strip()
            if not access_token:
                return _final_redirect(next_url, {"ok": "0", "error": "missing_access_token"})

            user_info = _http_json(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except urllib.error.HTTPError:
            return _final_redirect(next_url, {"ok": "0", "error": "oauth_exchange_failed"})
        except Exception:
            return _final_redirect(next_url, {"ok": "0", "error": "oauth_exchange_failed"})

        integration, _ = CloudIntegration.objects.get_or_create(
            user_id=user_id,
            provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE,
        )
        integration.account_email = user_info.get("email", "") or integration.account_email
        integration.access_token = access_token or integration.access_token
        incoming_refresh_token = (token_data.get("refresh_token") or "").strip()
        if incoming_refresh_token:
            integration.refresh_token = incoming_refresh_token
        expires_in = int(token_data.get("expires_in") or 3600)
        integration.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        integration.metadata = {
            **(integration.metadata or {}),
            "scope": token_data.get("scope", "") or integration.metadata.get("scope", ""),
            "token_type": token_data.get("token_type", ""),
        }
        integration.save()

        return _final_redirect(next_url, {"ok": "1", "provider": CloudIntegration.PROVIDER_GOOGLE_DRIVE})


class GoogleOAuthStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        integration = CloudIntegration.objects.filter(
            user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE
        ).first()
        if not integration:
            return Response({"ok": True, "connected": False, "provider": CloudIntegration.PROVIDER_GOOGLE_DRIVE})

        scopes = integration.metadata.get("scope", "") if integration.metadata else ""
        return Response(
            {
                "ok": True,
                "connected": bool((integration.access_token or "").strip()),
                "provider": integration.provider,
                "account_email": integration.account_email,
                "token_expires_at": integration.token_expires_at,
                "has_refresh_token": bool((integration.refresh_token or "").strip()),
                "scope": scopes,
            }
        )


class GoogleOAuthDisconnectAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).delete()
        return Response({"ok": True})


def _gmail_integration_or_error(user):
    integration = CloudIntegration.objects.filter(
        user=user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE
    ).first()
    if not integration or not integration.access_token:
        return None, {
            "ok": False,
            "error": "Conecta tu cuenta de Google primero para usar Gmail.",
            "reconnect_required": True,
        }
    scopes = integration.metadata.get("scope", "") if integration.metadata else ""
    scopes_lower = (scopes or "").lower()
    if "gmail" not in scopes_lower:
        return None, {
            "ok": False,
            "error": "Se requieren permisos de Gmail. Desconecta y vuelve a conectar tu cuenta de Google.",
            "reconnect_required": True,
        }
    return integration, None


def _gmail_headers_dict(message_payload: dict) -> dict:
    headers = {}
    for h in (message_payload.get("headers") or []):
        name = (h.get("name") or "").strip()
        value = h.get("value")
        if name:
            headers[name] = value
    return headers


def _gmail_decode_body(payload: dict) -> tuple[str, str]:
    def _decode_piece(data: str) -> str:
        if not data:
            return ""
        pad = "=" * (-len(data) % 4)
        try:
            return base64.urlsafe_b64decode((data + pad).encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    text_body = ""
    html_body = ""

    if "parts" in payload:
        for part in payload.get("parts") or []:
            mime = part.get("mimeType")
            body = part.get("body") or {}
            data = body.get("data")
            if mime == "text/plain" and isinstance(data, str):
                text_body += _decode_piece(data)
            elif mime == "text/html" and isinstance(data, str):
                html_body += _decode_piece(data)
            elif "parts" in part:
                sub_text, sub_html = _gmail_decode_body(part)
                text_body += sub_text
                html_body += sub_html
    else:
        body = payload.get("body") or {}
        data = body.get("data")
        mime = payload.get("mimeType")
        if isinstance(data, str):
            if mime == "text/html":
                html_body = _decode_piece(data)
            else:
                text_body = _decode_piece(data)

    return text_body, html_body


class GoogleGmailMessagesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        integration, error = _gmail_integration_or_error(request.user)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        access_token = _google_drive_refresh_token(integration)
        max_results_raw = (request.query_params.get("maxResults") or "").strip()
        q = (request.query_params.get("q") or "").strip()
        page_token = (request.query_params.get("pageToken") or "").strip()

        max_results = 20
        if max_results_raw:
            try:
                max_results = int(max_results_raw)
            except Exception:
                max_results = 20
        max_results = max(1, min(max_results, 50))

        qp = {"maxResults": str(max_results)}
        if q:
            qp["q"] = q
        if page_token:
            qp["pageToken"] = page_token

        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)
        try:
            response = _http_json(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except urllib.error.HTTPError:
            return Response({"ok": False, "error": "Error al consultar Gmail."}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception:
            return Response({"ok": False, "error": "Error inesperado al consultar Gmail."}, status=status.HTTP_502_BAD_GATEWAY)

        messages_list = response.get("messages") or []
        items = []
        for msg in messages_list:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            try:
                msg_detail = _http_json(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date&metadataHeaders=To",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                headers = _gmail_headers_dict((msg_detail.get("payload") or {}))
                from_header = headers.get("From") or ""
                from_name = from_header.split("<")[0].strip().strip('"\'') if "<" in from_header else from_header
                items.append(
                    {
                        "id": msg_detail.get("id") or msg_id,
                        "threadId": msg_detail.get("threadId"),
                        "snippet": msg_detail.get("snippet", ""),
                        "subject": headers.get("Subject", "(Sin asunto)"),
                        "from": from_name,
                        "from_full": from_header,
                        "to": headers.get("To", ""),
                        "date": headers.get("Date", ""),
                    }
                )
            except Exception:
                items.append({"id": msg_id})

        return Response(
            {
                "ok": True,
                "messages": items,
                "nextPageToken": response.get("nextPageToken"),
                "resultSizeEstimate": response.get("resultSizeEstimate"),
            }
        )


class GoogleGmailMessageDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, msg_id: str):
        integration, error = _gmail_integration_or_error(request.user)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        access_token = _google_drive_refresh_token(integration)
        try:
            msg_detail = _http_json(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except urllib.error.HTTPError:
            return Response({"ok": False, "error": "No pude cargar el correo."}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception:
            return Response({"ok": False, "error": "Error inesperado al cargar el correo."}, status=status.HTTP_502_BAD_GATEWAY)

        payload = msg_detail.get("payload") or {}
        headers = _gmail_headers_dict(payload)
        from_header = headers.get("From") or ""
        from_name = from_header.split("<")[0].strip().strip('"\'') if "<" in from_header else from_header
        text_body, html_body = _gmail_decode_body(payload)

        return Response(
            {
                "ok": True,
                "email": {
                    "id": msg_detail.get("id") or msg_id,
                    "threadId": msg_detail.get("threadId"),
                    "labelIds": msg_detail.get("labelIds") or [],
                    "snippet": msg_detail.get("snippet", ""),
                    "subject": headers.get("Subject", "(Sin asunto)"),
                    "from": from_name,
                    "from_full": from_header,
                    "to": headers.get("To", ""),
                    "date": headers.get("Date", ""),
                    "body_text": text_body,
                    "body_html": html_body,
                },
            }
        )


class GoogleGmailSendAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        integration, error = _gmail_integration_or_error(request.user)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        access_token = _google_drive_refresh_token(integration)

        to_email = (request.data.get("to") or "").strip()
        subject = (request.data.get("subject") or "(Sin asunto)").strip()
        body = request.data.get("body") or ""
        html_body = request.data.get("html") or ""

        if not to_email or not str(body).strip():
            return Response({"ok": False, "error": "to y body son requeridos."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            message = EmailMessage()
            message["To"] = to_email
            message["From"] = integration.account_email or ""
            message["Subject"] = subject

            if html_body:
                message.set_content(str(body) or "Cotización adjunta.")
                message.add_alternative(str(html_body), subtype='html')
            else:
                message.set_content(str(body))

            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

            sent = _http_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                method="POST",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                json_data={"raw": encoded_message},
            )
        except urllib.error.HTTPError:
            return Response({"ok": False, "error": "Error al enviar el correo."}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception:
            return Response({"ok": False, "error": "Error inesperado al enviar el correo."}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"ok": True, "message": {"id": sent.get("id"), "threadId": sent.get("threadId")}}, status=status.HTTP_201_CREATED)
