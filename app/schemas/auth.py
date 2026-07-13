# app/schemas/auth.py
"""
Esquemas Pydantic para la autenticación y autorización en el sistema.

Este módulo define todos los esquemas de validación, creación y lectura
relacionados con la autenticación de usuarios, tokens JWT y datos de sesión.

Características principales:
- Validaciones robustas con mensajes de error en español
- Seguridad en el manejo de tokens y datos de usuario
- Compatibilidad con OAuth2 y JWT
- Documentación clara para desarrolladores
"""

from pydantic import BaseModel, Field, field_validator, EmailStr
from typing import List, Optional
import re

class UserDataBase(BaseModel):
    """
    Schema base para datos de usuario en respuestas de autenticación.
    
    Incluye los datos básicos del usuario que se necesitan en el frontend
    para mostrar información y tomar decisiones de UI/UX.
    """
    
    usuario_id: int = Field(
        ...,
        description="Identificador único del usuario en el sistema",
        examples=[1, 2, 3]
    )
    
    nombre_usuario: str = Field(
        ...,
        description="Nombre de usuario único para identificación en el sistema",
        examples=["juan_perez", "maria.garcia"]
    )
    
    correo: Optional[str] = Field(
        None,
        description="Dirección de correo electrónico válida del usuario (opcional, puede estar vacío para usuarios cliente)",
        examples=["usuario@empresa.com", "nombre.apellido@dominio.org", None]
    )
    
    @field_validator('correo')
    @classmethod
    def validar_correo(cls, valor: Optional[str]) -> Optional[str]:
        """
        Valida y normaliza el correo electrónico.
        Convierte cadenas vacías a None y valida formato básico si tiene valor.
        """
        if valor is None:
            return None
        
        valor = valor.strip()
        
        if valor == '':
            return None
        
        # Validar formato básico de email solo si tiene valor
        # Usamos validación simple para permitir correos válidos pero ser permisivo con formatos no estándar
        if '@' not in valor:
            # Si no tiene @, retornar None en lugar de fallar (para usuarios cliente sin correo)
            return None
        
        return valor
    
    nombre: Optional[str] = Field(
        None,
        description="Nombre real del usuario",
        examples=["Juan", "María José"]
    )
    
    apellido: Optional[str] = Field(
        None,
        description="Apellido del usuario", 
        examples=["Pérez García", "López"]
    )
    
    es_activo: bool = Field(
        ...,
        description="Indica si el usuario está activo en el sistema",
        examples=[True, False]
    )

    codigo_trabajador_externo: Optional[str] = Field(
        None, 
        max_length=25, 
        description="Código de trabajador del sistema externo para sincronización de perfil."
    )
    
    tipo_trabajador: Optional[str] = Field(
        None,
        max_length=10,
        description="Tipo de trabajador (ctptra) del sistema cliente."
    )
    
    descripcion_usuario: Optional[str] = Field(
        None,
        max_length=100,
        description="Descripción del usuario (dusuar) del sistema cliente."
    )
    
    area: Optional[str] = Field(
        None,
        max_length=50,
        description="Área del trabajador del sistema cliente."
    )
    
    cargo: Optional[str] = Field(
        None,
        max_length=50,
        description="Cargo del trabajador del sistema cliente."
    )

    telefono: Optional[str] = Field(
        None,
        max_length=20,
        description="Teléfono de contacto del usuario (proveniente del sistema cliente)."
    )

    permiso_remoto: bool = Field(
        False,
        description="Indica si el usuario tiene permiso para realizar marcaciones remotas (mapeado desde usuarios_web00.permiso_remoto)."
    )

    @field_validator('nombre_usuario')
    @classmethod
    def validar_nombre_usuario(cls, valor: str) -> str:
        """
        Valida que el nombre de usuario tenga un formato básico.
        
        Reglas:
        - No puede estar vacío
        - Debe tener al menos 3 caracteres
        - Solo permite letras, números y guiones bajos
        
        Args:
            valor: El nombre de usuario a validar
            
        Returns:
            str: Nombre de usuario validado y normalizado
            
        Raises:
            ValueError: Cuando el formato no es válido
        """
        if not valor:
            raise ValueError('El nombre de usuario no puede estar vacío')
        
        valor = valor.strip()
        
        if len(valor) < 3:
            raise ValueError('El nombre de usuario debe tener al menos 3 caracteres')
        
        if not re.match(r'^[a-zA-Z0-9_]+$', valor):
            raise ValueError(
                'El nombre de usuario solo puede contener letras, números y guiones bajos (_). '
                'No se permiten espacios ni caracteres especiales.'
            )
        
        return valor

    @field_validator('nombre', 'apellido')
    @classmethod
    def validar_nombre_apellido(cls, valor: Optional[str]) -> Optional[str]:
        """
        Valida que nombres y apellidos contengan solo caracteres alfabéticos válidos.
        
        Permite:
        - Letras del alfabeto español (incluyendo ñ y acentos)
        - Espacios para nombres compuestos
        - Guiones para nombres compuestos
        
        Args:
            valor: El nombre o apellido a validar
            
        Returns:
            Optional[str]: Nombre o apellido validado y formateado
            
        Raises:
            ValueError: Cuando contiene caracteres no permitidos
        """
        if valor is None or valor == "":
            return None
        
        valor = valor.strip()
        
        if not valor:
            return None
        
        # Patrón que permite letras, espacios, guiones y caracteres españoles
        if not re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s\-]+$', valor):
            raise ValueError(
                'El nombre y apellido solo pueden contener letras, espacios y guiones. '
                'No se permiten números ni caracteres especiales.'
            )
        
        # Validar que no sea solo espacios o guiones
        if valor.replace(' ', '').replace('-', '') == '':
            raise ValueError('El nombre no puede contener solo espacios o guiones')
        
        # Formatear con capitalización adecuada
        return valor.title()

class UserDataWithRoles(UserDataBase):
    """
    Schema extendido para datos de usuario que incluye roles.
    
    Utilizado en respuestas de login y perfil de usuario donde
    se necesita información completa de roles y permisos.
    """
    
    roles: List[str] = Field(
        default_factory=list,
        description="Lista de nombres de roles asignados al usuario",
        examples=[["Administrador", "Usuario"], ["Reportes"]]
    )

    @field_validator('roles')
    @classmethod
    def validar_roles(cls, valor: List[str]) -> List[str]:
        """
        Valida que la lista de roles contenga solo strings no vacíos.
        
        Args:
            valor: Lista de nombres de roles a validar
            
        Returns:
            List[str]: Lista de roles validada
            
        Raises:
            ValueError: Cuando algún rol no es válido
        """
        if not isinstance(valor, list):
            raise ValueError('Los roles deben ser una lista')
        
        roles_validos = []
        for rol in valor:
            if not isinstance(rol, str):
                raise ValueError('Cada rol debe ser una cadena de texto')
            
            rol_limpio = rol.strip()
            if not rol_limpio:
                raise ValueError('Los nombres de roles no pueden estar vacíos')
            
            roles_validos.append(rol_limpio)
        
        return roles_validos

class LoginData(BaseModel):
    """
    Schema para datos de inicio de sesión.
    
    Alternativa al uso de OAuth2PasswordRequestForm para casos
    donde se necesite más control sobre la validación.
    """
    
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="Nombre de usuario para autenticación",
        examples=["juan_perez", "maria.garcia"]
    )
    
    password: str = Field(
        ...,
        min_length=1,
        description="Contraseña del usuario",
        examples=["MiContraseñaSegura123", "OtraPassword123!"]
    )

    @field_validator('username')
    @classmethod
    def validar_username_login(cls, valor: str) -> str:
        """
        Valida el formato del nombre de usuario durante el login.
        
        Args:
            valor: Nombre de usuario a validar
            
        Returns:
            str: Nombre de usuario validado
            
        Raises:
            ValueError: Cuando el formato no es válido
        """
        if not valor:
            raise ValueError('El nombre de usuario no puede estar vacío')
        
        valor = valor.strip()
        
        if len(valor) < 3:
            raise ValueError('El nombre de usuario debe tener al menos 3 caracteres')
        
        return valor

    @field_validator('password')
    @classmethod
    def validar_password_login(cls, valor: str) -> str:
        """
        Valida que la contraseña no esté vacía.
        
        Nota: No validamos fortaleza aquí porque eso se hace al crear la cuenta.
        
        Args:
            valor: Contraseña a validar
            
        Returns:
            str: Contraseña validada
            
        Raises:
            ValueError: Cuando la contraseña está vacía
        """
        if not valor:
            raise ValueError('La contraseña no puede estar vacía')
        
        return valor

class Token(BaseModel):
    """
    Schema para respuesta de tokens JWT.
    
    Incluye el token de acceso y opcionalmente los datos del usuario
    para evitar llamadas adicionales al backend después del login.
    """
    
    access_token: str = Field(
        ...,
        description="Token JWT de acceso para autorizar solicitudes",
        examples=["eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."]
    )
    
    refresh_token: Optional[str] = Field(
        None,
        description="Refresh Token JWT (solo para clientes mobile o cuando se requiera persistencia explícita)",
        examples=["eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."]
    )

    token_type: str = Field(
        "bearer",
        description="Tipo de token, siempre 'bearer' para JWT",
        examples=["bearer"]
    )
    
    user_data: Optional[UserDataWithRoles] = Field(
        None,
        description="Datos del usuario autenticado (opcional, no se incluye en refresh)"
    )

    @field_validator('access_token')
    @classmethod
    def validar_access_token(cls, valor: str) -> str:
        """
        Valida que el token no esté vacío.
        
        Args:
            valor: Token JWT a validar
            
        Returns:
            str: Token validado
            
        Raises:
            ValueError: Cuando el token está vacío
        """
        if not valor:
            raise ValueError('El token de acceso no puede estar vacío')
        
        return valor

    @field_validator('refresh_token')
    @classmethod
    def validar_refresh_token(cls, valor: Optional[str]) -> Optional[str]:
        if valor is None:
            return None
        if not valor:
            raise ValueError('El refresh token no puede estar vacío')
        return valor

class TokenPayload(BaseModel):
    """
    Schema para el payload de tokens JWT.
    
    Representa la estructura de datos codificada dentro de los tokens JWT
    generados por el sistema.
    """
    
    sub: Optional[str] = Field(
        None,
        description="Subject (usualmente el nombre de usuario)",
        examples=["juan_perez", "maria.garcia"]
    )
    
    exp: Optional[int] = Field(
        None,
        description="Timestamp de expiración del token",
        examples=[1735689600]
    )
    
    iat: Optional[int] = Field(
        None, 
        description="Timestamp de emisión del token",
        examples=[1735689000]
    )
    
    type: Optional[str] = Field(
        None,
        description="Tipo de token: 'access' o 'refresh'",
        examples=["access", "refresh"]
    )

    @field_validator('type')
    @classmethod
    def validar_tipo_token(cls, valor: Optional[str]) -> Optional[str]:
        """
        Valida que el tipo de token sea uno de los permitidos.
        
        Args:
            valor: Tipo de token a validar
            
        Returns:
            Optional[str]: Tipo de token validado
            
        Raises:
            ValueError: Cuando el tipo no es válido
        """
        if valor is not None and valor not in ['access', 'refresh']:
            raise ValueError('El tipo de token debe ser "access" o "refresh"')
        
        return valor

    class Config:
        """Configuración de Pydantic para el schema."""
        from_attributes = True
        str_strip_whitespace = True
        validate_assignment = True