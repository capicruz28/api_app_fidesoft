# app/core/auth.py
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
import logging
import hashlib
import uuid

from fastapi import Depends, HTTPException, status, Cookie
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.core.config import settings
from app.core.security import verify_password
from app.db.queries import (
    execute_auth_query, 
    execute_query, 
    execute_insert,
    execute_update,
    AUTHENTICATE_CLIENTE_USER,
    SELECT_CLIENTE_USER_DATA,
    INSERT_USUARIO_FROM_CLIENTE
)
from app.schemas.auth import TokenPayload

logger = logging.getLogger(__name__)

# Swagger/OpenAPI: flujo password con tokenUrl en /api/v1
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="apivac/v1/auth/login/")


def create_access_token(data: dict) -> str:
    """
    Crea un token JWT de acceso con iat, exp y type='access'
    """
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({
        "exp": expire,
        "iat": now,
        "type": "access",
    })
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """
    Crea un token JWT de refresh con iat, exp y type='refresh'
    """
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # Siempre incluir un identificador único (jti) para soportar rotación y revocación real.
    # Mantener compatibilidad: si el caller ya envía jti, se respeta.
    jti = to_encode.get("jti") or uuid.uuid4().hex
    to_encode.update({
        "exp": expire,
        "iat": now,
        "type": "refresh",
        "jti": jti,
    })
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token_with_meta(data: dict) -> Tuple[str, str, datetime]:
    """
    Crea refresh token y retorna (token, jti, expires_at_utc).
    """
    token = create_refresh_token(data=data)
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        jti = str(payload.get("jti") or "")
        exp = payload.get("exp")
        if not jti or exp is None:
            raise JWTError("refresh token missing jti/exp")
        expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc)
        return token, jti, expires_at
    except Exception as e:
        logger.error(f"Error generando metadata de refresh token: {str(e)}")
        raise


def hash_token(token: str) -> str:
    """
    Hash del refresh token (no guardar en texto plano).
    SHA-256 hex.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_client_type(client_type: Optional[str]) -> str:
    ct = (client_type or "").strip().lower()
    if ct == "mobile":
        return "mobile"
    return "web"


def save_refresh_token(
    *,
    usuario_id: int,
    refresh_token: str,
    expires_at: datetime,
    client_type: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> None:
    token_hash = hash_token(refresh_token)
    ct = _parse_client_type(client_type)
    # expires_at viene en UTC; SQL Server almacenará DATETIME (sin tz). Guardamos como naive UTC.
    expires_at_naive = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    query = """
        INSERT INTO refresh_tokens (
            usuario_id, token_hash, expires_at, is_revoked, revoked_at, created_at,
            client_type, ip_address, user_agent
        )
        VALUES (?, ?, ?, 0, NULL, GETDATE(), ?, ?, ?);
    """
    execute_insert(
        query,
        (
            usuario_id,
            token_hash,
            expires_at_naive,
            ct,
            (ip_address or "")[:45],
            (user_agent or "")[:512],
        ),
    )


def get_refresh_token_record(token_hash: str) -> Optional[Dict[str, Any]]:
    query = """
        SELECT TOP 1
            token_id, usuario_id, token_hash, expires_at, is_revoked, revoked_at, created_at,
            client_type, ip_address, user_agent
        FROM refresh_tokens
        WHERE token_hash = ?;
    """
    return execute_auth_query(query, (token_hash,))


def revoke_refresh_token(token_hash: str) -> None:
    query = """
        UPDATE refresh_tokens
        SET is_revoked = 1,
            revoked_at = COALESCE(revoked_at, GETDATE())
        WHERE token_hash = ? AND is_revoked = 0;
    """
    execute_update(query, (token_hash,))


def validate_refresh_token(refresh_token: str) -> Dict[str, Any]:
    """
    Valida refresh token (JWT) + existencia/estado en BD (hash).
    Retorna payload si es válido.
    """
    # 1) Validación criptográfica y claims
    payload = decode_refresh_token(refresh_token)
    exp = payload.get("exp")
    if exp is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2) Validación en BD (hash)
    token_hash = hash_token(refresh_token)
    record = get_refresh_token_record(token_hash)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if record.get("is_revoked"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # expires_at en BD (naive) asumimos UTC. Comparar con utcnow naive.
    expires_at = record.get("expires_at")
    if expires_at and isinstance(expires_at, datetime):
        if expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return payload

def decode_refresh_token(token: str) -> dict:
    """
    Decodifica y valida un refresh token (type='refresh')
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            raise JWTError("Token type is not refresh")
        return payload
    except JWTError as e:
        logger.error(f"Error decodificando refresh token: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _authenticate_local_user(username: str, password: str) -> Optional[Dict]:
    """
    Autentica un usuario local (origen_datos='local') contra la tabla usuario.
    
    Returns:
        Dict con datos del usuario si las credenciales son correctas, None si no existe o credenciales incorrectas
    """
    try:
        query = """
            SELECT usuario_id, nombre_usuario, correo, contrasena,
                   nombre, apellido, es_activo, codigo_trabajador_externo,
                   origen_datos
            FROM usuario
            WHERE nombre_usuario = ? AND es_eliminado = 0 AND origen_datos = 'local'
        """
        user = execute_auth_query(query, (username,))

        if not user:
            return None

        if not verify_password(password, user['contrasena']):
            return None

        if not user['es_activo']:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario inactivo"
            )

        # Actualizar fecha último acceso
        update_query = """
            UPDATE usuario
            SET fecha_ultimo_acceso = GETDATE()
            WHERE usuario_id = ?
        """
        execute_auth_query(update_query, (user['usuario_id'],))

        # Eliminar la contraseña del resultado
        user.pop('contrasena', None)
        
        # Normalizar correo vacío a None
        if user.get('correo') == '':
            user['correo'] = None
        
        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en autenticación local: {str(e)}", exc_info=True)
        return None


async def _authenticate_cliente_user(username: str, password: str) -> Optional[Dict]:
    """
    Autentica un usuario contra la tabla usuarios_web00 (contraseña en texto plano).
    
    Returns:
        Dict con datos básicos del usuario si las credenciales son correctas, None si no existe o credenciales incorrectas
    """
    try:
        cliente_user = execute_auth_query(AUTHENTICATE_CLIENTE_USER, (username,))
        
        if not cliente_user:
            return None

        # Verificar contraseña en texto plano
        if cliente_user['contrasena'] != password:
            return None

        # Buscar si el usuario ya existe en la tabla usuario
        query_existing = """
            SELECT usuario_id, nombre_usuario, correo, nombre, apellido, es_activo,
                   codigo_trabajador_externo, origen_datos
            FROM usuario
            WHERE nombre_usuario = ? AND es_eliminado = 0 AND origen_datos = 'cliente'
        """
        existing_user = execute_auth_query(query_existing, (username,))

        if existing_user:
            # Usuario ya existe, verificar que esté activo
            if not existing_user['es_activo']:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Usuario inactivo"
                )
            
            # Actualizar fecha último acceso
            update_query = """
                UPDATE usuario
                SET fecha_ultimo_acceso = GETDATE()
                WHERE usuario_id = ?
            """
            execute_auth_query(update_query, (existing_user['usuario_id'],))
            
            # Obtener datos adicionales desde tablas del cliente para la respuesta
            user_data_cliente = execute_auth_query(SELECT_CLIENTE_USER_DATA, (username,))
            if user_data_cliente:
                existing_user['tipo_trabajador'] = user_data_cliente.get('tipo_trabajador')
                existing_user['descripcion_usuario'] = user_data_cliente.get('descripcion_usuario')
                existing_user['area'] = user_data_cliente.get('area')
                existing_user['cargo'] = user_data_cliente.get('cargo')
                existing_user['telefono'] = user_data_cliente.get('telefono')
            
            # Normalizar correo vacío a None
            if existing_user.get('correo') == '':
                existing_user['correo'] = None
            
            return existing_user
        else:
            # Usuario no existe en tabla usuario, crear registro (primer login)
            logger.info(f"Primer login de usuario cliente '{username}', creando registro en tabla usuario")
            return await _create_usuario_from_cliente(username, cliente_user['codigo_trabajador'])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en autenticación cliente: {str(e)}", exc_info=True)
        return None


async def _create_usuario_from_cliente(username: str, codigo_trabajador: str) -> Dict:
    """
    Crea un registro en la tabla usuario desde datos de usuarios_web00 y mtraba_web00.
    Se ejecuta en el primer login de un usuario cliente.
    
    Args:
        username: Nombre de usuario (cusuar)
        codigo_trabajador: Código de trabajador (ctraba)
        
    Returns:
        Dict con los datos del usuario creado
        
    Raises:
        HTTPException: Si hay errores al obtener datos o crear el usuario
    """
    try:
        # Obtener datos completos del usuario desde las tablas del cliente
        logger.debug(f"Obteniendo datos completos del usuario '{username}' desde tablas del cliente")
        user_data = execute_auth_query(SELECT_CLIENTE_USER_DATA, (username,))
        
        if not user_data:
            logger.error(f"No se encontraron datos en SELECT_CLIENTE_USER_DATA para usuario '{username}'")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No se encontraron datos del usuario en el sistema del cliente"
            )

        logger.debug(f"Datos obtenidos del cliente para '{username}': {list(user_data.keys())}")

        # Crear registro en tabla usuario (sin campos adicionales que no están en la tabla)
        # Nota: No guardamos la contraseña porque usuarios cliente se autentican contra usuarios_web00
        # Los campos adicionales (tipo_trabajador, descripcion_usuario, area, cargo) se obtienen
        # desde las tablas del cliente pero NO se guardan en usuario
        params = (
            user_data['nombre_usuario'],
            user_data.get('correo') or '',  # Puede ser NULL
            user_data.get('nombre') or '',
            user_data.get('apellido') or '',
            codigo_trabajador
        )
        
        logger.debug(f"Insertando usuario con parámetros: nombre_usuario={params[0]}, correo={params[1]}, nombre={params[2]}, apellido={params[3]}, codigo_trabajador={params[4]}")
        
        result = execute_insert(INSERT_USUARIO_FROM_CLIENTE, params)
        
        if not result:
            logger.error(f"execute_insert retornó None para usuario '{username}'")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error al crear registro de usuario"
            )

        logger.debug(f"Usuario creado exitosamente: {result.get('usuario_id')}")

        # Agregar campos adicionales desde las tablas del cliente para la respuesta (no se guardan en usuario)
        result['tipo_trabajador'] = user_data.get('tipo_trabajador')
        result['descripcion_usuario'] = user_data.get('descripcion_usuario')
        result['area'] = user_data.get('area')
        result['cargo'] = user_data.get('cargo')
        result['telefono'] = user_data.get('telefono')
        
        # Normalizar correo vacío a None
        if result.get('correo') == '':
            result['correo'] = None

        logger.info(f"Usuario cliente '{username}' creado exitosamente con ID: {result.get('usuario_id')}")
        return result

    except HTTPException:
        # Re-lanzar HTTPException para que se propague
        raise
    except Exception as e:
        logger.error(f"Error creando usuario desde cliente: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al crear registro de usuario desde sistema cliente: {str(e)}"
        )


async def authenticate_user(username: str, password: str) -> Dict:
    """
    Autentica un usuario detectando automáticamente su origen de datos.
    
    Flujo:
    1. Si el usuario existe en tabla usuario con origen_datos='local' → autenticar contra tabla usuario
    2. Si el usuario existe en tabla usuario con origen_datos='cliente' → autenticar contra usuarios_web00
    3. Si el usuario NO existe en tabla usuario → autenticar contra usuarios_web00 y crear registro si es exitoso
    
    Args:
        username: Nombre de usuario
        password: Contraseña
        
    Returns:
        Dict con datos del usuario autenticado (sin contraseña)
        
    Raises:
        HTTPException: Si las credenciales son incorrectas o el usuario está inactivo
    """
    try:
        # 1️⃣ Verificar si el usuario existe en tabla usuario
        query_check = """
            SELECT usuario_id, origen_datos, es_activo
            FROM usuario
            WHERE nombre_usuario = ? AND es_eliminado = 0
        """
        user_check = execute_auth_query(query_check, (username,))

        if user_check:
            # Usuario existe en tabla usuario
            origen = user_check.get('origen_datos', 'local')
            
            if origen == 'local':
                # 🔵 Autenticar como usuario local
                user = await _authenticate_local_user(username, password)
                if user:
                    return user
            elif origen == 'cliente':
                # 🟢 Autenticar contra usuarios_web00
                user = await _authenticate_cliente_user(username, password)
                if user:
                    return user
            else:
                logger.warning(f"Usuario '{username}' tiene origen_datos desconocido: {origen}")
        else:
            # 2️⃣ Usuario NO existe en tabla usuario → intentar autenticar contra usuarios_web00
            logger.info(f"Usuario '{username}' no existe en tabla usuario, intentando autenticación contra usuarios_web00")
            user = await _authenticate_cliente_user(username, password)
            if user:
                return user

        # Si llegamos aquí, las credenciales son incorrectas
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en autenticación: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error en el proceso de autenticación"
        )


async def get_current_user(token: str = Depends(oauth2_scheme)) -> Dict:
    """
    Obtiene el usuario actual basado en el access token (Bearer).
    - Valida algoritmo, firma y expiración
    - Requiere type='access'
    - Usa claim estándar 'sub' como nombre de usuario
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        token_data = TokenPayload(**payload)

        if not token_data.sub or token_data.type != "access":
            raise credentials_exception

        username = token_data.sub

    except JWTError as e:
        logger.error(f"Error decodificando token: {str(e)}")
        raise credentials_exception
    except Exception as e:
        logger.error(f"Error procesando payload del token: {str(e)}")
        raise credentials_exception

    query = """
        SELECT usuario_id, nombre_usuario, correo, nombre, apellido, es_activo, codigo_trabajador_externo,
               origen_datos
        FROM usuario
        WHERE nombre_usuario = ? AND es_eliminado = 0
    """
    
    user = execute_auth_query(query, (username,))

    if not user:
        raise credentials_exception

    if not user['es_activo']:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario inactivo"
        )

    # Si es usuario cliente, obtener campos adicionales desde tablas del cliente
    if user.get('origen_datos') == 'cliente':
        user_data_cliente = execute_auth_query(SELECT_CLIENTE_USER_DATA, (username,))
        if user_data_cliente:
            user['tipo_trabajador'] = user_data_cliente.get('tipo_trabajador')
            user['descripcion_usuario'] = user_data_cliente.get('descripcion_usuario')
            user['area'] = user_data_cliente.get('area')
            user['cargo'] = user_data_cliente.get('cargo')
            user['telefono'] = user_data_cliente.get('telefono')
    
    # Normalizar correo vacío a None
    if user.get('correo') == '':
        user['correo'] = None

    return user


async def get_current_user_from_refresh(
    refresh_token: Optional[str] = Cookie(None, alias=settings.REFRESH_COOKIE_NAME)
) -> Dict:
    """
    Obtiene el usuario actual validando el refresh token de la cookie
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token provided"
        )

    try:
        payload = decode_refresh_token(refresh_token)
        token_data = TokenPayload(**payload)

        if not token_data.sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token"
            )

        username = token_data.sub

        query = """
            SELECT usuario_id, nombre_usuario, correo, nombre, apellido, es_activo, codigo_trabajador_externo,
                   origen_datos
            FROM usuario
            WHERE nombre_usuario = ? AND es_eliminado = 0
        """
        user = execute_auth_query(query, (username,))

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario no encontrado"
            )

        if not user['es_activo']:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario inactivo"
            )

        # Si es usuario cliente, obtener campos adicionales desde tablas del cliente
        if user.get('origen_datos') == 'cliente':
            user_data_cliente = execute_auth_query(SELECT_CLIENTE_USER_DATA, (username,))
            if user_data_cliente:
                user['tipo_trabajador'] = user_data_cliente.get('tipo_trabajador')
                user['descripcion_usuario'] = user_data_cliente.get('descripcion_usuario')
                user['area'] = user_data_cliente.get('area')
                user['cargo'] = user_data_cliente.get('cargo')
                user['telefono'] = user_data_cliente.get('telefono')
        
        # Normalizar correo vacío a None
        if user.get('correo') == '':
            user['correo'] = None

        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validando refresh token: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
