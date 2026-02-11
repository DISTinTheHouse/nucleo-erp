from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from django.contrib.auth import authenticate
from rest_framework import status

class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        # Limpieza básica
        if email:
            email = email.strip()

        print(f"DEBUG LOGIN: Email recibido: '{email}'")
        # print(f"DEBUG LOGIN: Password recibido: '{password}'") # No imprimir passwords reales en logs

        if not email or not password:
            return Response({'error': 'Email y contraseña son requeridos'}, status=status.HTTP_400_BAD_REQUEST)

        # Autenticar usando el EmailBackend configurado en settings
        user = authenticate(request, email=email, password=password)

        if user:
            if not user.is_active or getattr(user, 'estatus', '') == 'bloqueado':
                return Response({'error': 'Su cuenta está bloqueada o inactiva. Contacte al administrador.'}, status=status.HTTP_403_FORBIDDEN)
            
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user_id': user.pk,
                'email': user.email,
                'username': user.username,
                'nombre_completo': f"{user.first_name} {user.last_name}".strip(),
                'es_admin': user.is_staff or user.is_superuser,
                'is_superuser': user.is_superuser,
                'is_admin_empresa': getattr(user, 'is_admin_empresa', False),
                'empresa_id': user.empresa_id if user.empresa else None
            })
        else:
            return Response({'error': 'Credenciales inválidas'}, status=status.HTTP_401_UNAUTHORIZED)
