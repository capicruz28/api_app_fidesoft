# app/db/queries.py
from typing import List, Dict, Any, Callable
from app.db.connection import get_db_connection, DatabaseConnection
from app.core.exceptions import DatabaseError
import pyodbc
import logging

logger = logging.getLogger(__name__)

def execute_query(query: str, params: tuple = (), connection_type: DatabaseConnection = DatabaseConnection.DEFAULT) -> List[Dict[str, Any]]:
    with get_db_connection(connection_type) as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error en execute_query: {str(e)}")
            raise DatabaseError(detail=f"Error en la consulta: {str(e)}")
        finally:
            cursor.close()

def execute_auth_query(query: str, params: tuple = ()) -> Dict[str, Any]:
    """
    Ejecuta una consulta específica para autenticación y retorna un único registro.
    Siempre usa la conexión DEFAULT ya que la autenticación está en la BD principal.
    """
    with get_db_connection(DatabaseConnection.DEFAULT) as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)

            if cursor.description is None:
                return None

            columns = [column[0] for column in cursor.description]
            row = cursor.fetchone()

            if row:
                return dict(zip(columns, row))
            return None

        except Exception as e:
            logger.error(f"Error en execute_auth_query: {str(e)}")
            raise DatabaseError(detail=f"Error en la autenticación: {str(e)}")
        finally:
            if cursor:
                cursor.close()

def execute_insert(query: str, params: tuple = (), connection_type: DatabaseConnection = DatabaseConnection.DEFAULT) -> Dict[str, Any]:
    """
    Ejecuta una sentencia INSERT y retorna:
      - Los datos retornados por OUTPUT si existen
      - Siempre incluye 'rows_affected' en la respuesta
    """
    with get_db_connection(connection_type) as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)

            # Verificar OUTPUT
            if cursor.description:  
                columns = [column[0] for column in cursor.description]
                output_data = cursor.fetchone()
                result = dict(zip(columns, output_data)) if output_data else {}
            else:
                result = {}

            # Importante: filas afectadas
            rows_affected = cursor.rowcount
            result["rows_affected"] = rows_affected

            conn.commit()
            logger.info(f"Inserción exitosa, filas afectadas: {rows_affected}")
            return result

        except Exception as e:
            conn.rollback()
            logger.error(f"Error en execute_insert: {str(e)}")
            raise DatabaseError(
                detail=f"Error en la inserción: {str(e)}"
            )
        finally:
            cursor.close()                

def execute_update(query: str, params: tuple = (), connection_type: DatabaseConnection = DatabaseConnection.DEFAULT) -> Dict[str, Any]:
    with get_db_connection(connection_type) as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            
            # Obtener número de filas afectadas
            rows_affected = cursor.rowcount
            
            # Si hay OUTPUT, obtener los datos
            if cursor.description:
                columns = [column[0] for column in cursor.description]
                output_data = cursor.fetchone()
                result = dict(zip(columns, output_data)) if output_data else {}
            else:
                result = {}
            
            conn.commit()
            
            # CAMBIO CLAVE: Siempre incluir rows_affected en la respuesta
            result['rows_affected'] = rows_affected
            
            logger.info(f"Actualización exitosa, filas afectadas: {rows_affected}")
            return result

        except Exception as e:
            conn.rollback()
            logger.error(f"Error en execute_update: {str(e)}")
            raise DatabaseError(
                detail=f"Error en la actualización: {str(e)}"
            )
        finally:
            cursor.close()

def execute_procedure(procedure_name: str, connection_type: DatabaseConnection = DatabaseConnection.DEFAULT) -> List[Dict[str, Any]]:
    with get_db_connection(connection_type) as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(f"EXEC {procedure_name}")

            results = []
            while True:
                if cursor.description:
                    columns = [column[0] for column in cursor.description]
                    results.extend([dict(zip(columns, row)) for row in cursor.fetchall()])
                if not cursor.nextset():
                    break
            return results
        except Exception as e:
            logger.error(f"Error en execute_procedure: {str(e)}")
            raise DatabaseError(detail=f"Error en el procedimiento: {str(e)}")
        finally:
            cursor.close()

def execute_procedure_params(
    procedure_name: str,
    params: dict,
    connection_type: DatabaseConnection = DatabaseConnection.DEFAULT
) -> List[Dict[str, Any]]:
    with get_db_connection(connection_type) as conn:
        try:
            cursor = conn.cursor()
            param_str = ", ".join([f"@{key} = ?" for key in params.keys()])
            query = f"EXEC {procedure_name} {param_str}"

            cursor.execute(query, tuple(params.values()))

            results = []
            while True:
                if cursor.description:
                    columns = [column[0] for column in cursor.description]
                    results.extend([dict(zip(columns, row)) for row in cursor.fetchall()])
                if not cursor.nextset():
                    break
            return results
        except Exception as e:
            logger.error(f"Error en execute_procedure_params: {str(e)}")
            raise DatabaseError(detail=f"Error en el procedimiento: {str(e)}")
        finally:
            cursor.close()

def execute_transaction(
    operations_func: Callable[[pyodbc.Cursor], None],
    connection_type: DatabaseConnection = DatabaseConnection.DEFAULT
) -> None:
    """
    Ejecuta operaciones de BD en una transacción.
    Maneja errores de conexión y operación de pyodbc.
    """
    conn = None
    cursor = None
    try:
        with get_db_connection(connection_type) as conn:
            cursor = conn.cursor()
            operations_func(cursor)
            conn.commit()
            logger.debug("Transacción completada exitosamente.")

    except pyodbc.Error as db_err:
        logger.error(f"Error de base de datos (pyodbc) en transacción: {db_err}", exc_info=True)
        raise DatabaseError(detail=f"Error DB en transacción: {str(db_err)}")

    except Exception as e:
        logger.error(f"Error inesperado (no pyodbc) en transacción: {e}", exc_info=True)
        raise DatabaseError(detail=f"Error inesperado en transacción: {str(e)}")

# Consulta para obtener usuarios paginados con sus roles, filtrando eliminados y buscando
# CORREGIDA: Usa DENSE_RANK() para paginar sobre usuarios únicos, no sobre filas con JOIN
SELECT_USUARIOS_PAGINATED = """
WITH UsuariosPaginados AS (
    SELECT
        u.usuario_id,
        ROW_NUMBER() OVER (ORDER BY u.usuario_id) AS rn
    FROM usuario u
    WHERE
        u.es_eliminado = 0
        AND (? IS NULL OR (
            u.nombre_usuario LIKE ? OR
            u.correo LIKE ? OR
            u.nombre LIKE ? OR
            u.apellido LIKE ?
        ))
),
UserRoles AS (
    SELECT
        u.usuario_id,
        u.nombre_usuario,
        u.correo,
        u.nombre,
        u.apellido,
        u.es_activo,
        u.correo_confirmado,
        u.fecha_creacion,
        u.fecha_ultimo_acceso,
        u.fecha_actualizacion,        
        u.origen_datos, 
        u.codigo_trabajador_externo,
        r.rol_id,
        r.nombre AS nombre_rol,
        up.rn
    FROM UsuariosPaginados up
    INNER JOIN usuario u ON up.usuario_id = u.usuario_id
    LEFT JOIN usuario_rol ur ON u.usuario_id = ur.usuario_id AND ur.es_activo = 1
    LEFT JOIN rol r ON ur.rol_id = r.rol_id AND r.es_activo = 1
    WHERE up.rn BETWEEN ? AND ?
)
SELECT *
FROM UserRoles
ORDER BY rn, rol_id;
"""

# Consulta para contar el total de usuarios que coinciden con la búsqueda y no están eliminados
COUNT_USUARIOS_PAGINATED = """
SELECT COUNT(DISTINCT u.usuario_id)
FROM usuario u
WHERE
    u.es_eliminado = 0
    AND (? IS NULL OR (
        u.nombre_usuario LIKE ? OR
        u.correo LIKE ? OR
        u.nombre LIKE ? OR
        u.apellido LIKE ?
    ));
"""

# --- Consultas de Roles (Existentes - SIN CAMBIOS) ---
# (Asumiendo que tienes aquí tus queries SELECT_ROL_BY_ID, INSERT_ROL, etc.)
# Si no las tienes, deberías añadirlas aquí. Por ejemplo:
SELECT_ROL_BY_ID = "SELECT rol_id, nombre, descripcion, es_activo, fecha_creacion FROM dbo.rol WHERE rol_id = ? AND es_activo = 1"
SELECT_ALL_ROLES = "SELECT rol_id, nombre, descripcion, es_activo, fecha_creacion FROM dbo.rol WHERE es_activo = 1 ORDER BY nombre"
INSERT_ROL = "INSERT INTO dbo.rol (nombre, descripcion, es_activo) OUTPUT INSERTED.rol_id, INSERTED.nombre, INSERTED.descripcion, INSERTED.es_activo, INSERTED.fecha_creacion VALUES (?, ?, ?)"
UPDATE_ROL = "UPDATE dbo.rol SET nombre = ?, descripcion = ?, es_activo = ? OUTPUT INSERTED.rol_id, INSERTED.nombre, INSERTED.descripcion, INSERTED.es_activo, INSERTED.fecha_creacion WHERE rol_id = ?"
# Nota: DEACTIVATE_ROL podría ser un caso especial de UPDATE_ROL o una query separada
DEACTIVATE_ROL = """
    UPDATE dbo.rol
    SET
        es_activo = 0
    OUTPUT
        INSERTED.rol_id,
        INSERTED.nombre,
        INSERTED.descripcion,
        INSERTED.es_activo,
        INSERTED.fecha_creacion
    WHERE
        rol_id = ?
        AND es_activo = 1;  -- Solo desactivar si está activo
"""
REACTIVATE_ROL = """
    UPDATE dbo.rol
    SET
        es_activo = 1
    OUTPUT
        INSERTED.rol_id,
        INSERTED.nombre,
        INSERTED.descripcion,
        INSERTED.es_activo,
        INSERTED.fecha_creacion
    WHERE
        rol_id = ?
        AND es_activo = 0;  -- Solo reactivar si está inactivo
"""
CHECK_ROL_NAME_EXISTS = "SELECT rol_id FROM dbo.rol WHERE LOWER(nombre) = LOWER(?) AND rol_id != ?"


# --- NUEVAS QUERIES PARA PAGINACIÓN DE ROLES ---
COUNT_ROLES_PAGINATED = """
    SELECT COUNT(rol_id) as total -- Añadir alias 'total' para consistencia
    FROM dbo.rol
    WHERE (? IS NULL OR (
        LOWER(nombre) LIKE LOWER(?) OR
        LOWER(descripcion) LIKE LOWER(?)
    ));
    -- Nota: No filtra por es_activo aquí para mostrar todos en mantenimiento
    -- Usamos LOWER() para búsqueda insensible a mayúsculas/minúsculas
"""

SELECT_ROLES_PAGINATED = """
    WITH RolPaginado AS (
    SELECT
        rol_id,
        nombre,
        descripcion,
        es_activo,
        fecha_creacion,
        ROW_NUMBER() OVER (ORDER BY rol_id) AS rn
    FROM
        dbo.rol
    WHERE (? IS NULL OR (
        LOWER(nombre) LIKE LOWER(?) OR
        LOWER(descripcion) LIKE LOWER(?)
    ))
)
SELECT
    rol_id,
    nombre,
    descripcion,
    es_activo,
    fecha_creacion
FROM RolPaginado
WHERE rn BETWEEN ? AND ?;
    -- Nota: No filtra por es_activo aquí
    -- Usamos LOWER() para búsqueda insensible a mayúsculas/minúsculas
"""
# --- FIN NUEVAS QUERIES ---

# --- NUEVA CONSULTA PARA MENUS (ADMIN) ---
# Llama a la nueva Stored Procedure que obtiene TODOS los menús
GET_ALL_MENUS_ADMIN = "sp_GetAllMenuItemsAdmin;"


# --- NUEVAS CONSULTAS PARA PERMISOS (RolMenuPermiso) ---

# Selecciona todos los permisos asignados a un rol específico
SELECT_PERMISOS_POR_ROL = """
    SELECT rol_menu_id, rol_id, menu_id, puede_ver, puede_editar, puede_eliminar
    FROM rol_menu_permiso
    WHERE rol_id = ?;
"""

# Elimina TODOS los permisos asociados a un rol específico.
# Se usa antes de insertar los nuevos permisos actualizados.
DELETE_PERMISOS_POR_ROL = """
    DELETE FROM rol_menu_permiso
    WHERE rol_id = ?;
"""

# Inserta un nuevo registro de permiso para un rol y un menú.
# Los parámetros serán (rol_id, menu_id, puede_ver, puede_editar, puede_eliminar)
INSERT_PERMISO_ROL = """
    INSERT INTO rol_menu_permiso (rol_id, menu_id, puede_ver, puede_editar, puede_eliminar)
    VALUES (?, ?, ?, ?, ?);
"""

# --- FIN DE NUEVAS CONSULTAS ---

# --- NUEVAS QUERIES PARA MANTENIMIENTO DE MENÚ ---

INSERT_MENU = """
    INSERT INTO menu (nombre, icono, ruta, padre_menu_id, orden, area_id, es_activo)
    OUTPUT INSERTED.menu_id, INSERTED.nombre, INSERTED.icono, INSERTED.ruta,
           INSERTED.padre_menu_id, INSERTED.orden, INSERTED.es_activo, INSERTED.area_id,
           INSERTED.fecha_creacion -- Añadir fecha_creacion si la quieres devolver
           -- , a.nombre as area_nombre -- No podemos hacer JOIN fácil en INSERT OUTPUT
    VALUES (?, ?, ?, ?, ?, ?, ?);
"""

# Selecciona un menú por ID, incluyendo el nombre del área
SELECT_MENU_BY_ID = """
    SELECT m.menu_id, m.nombre, m.icono, m.ruta, m.padre_menu_id, m.orden,
           m.es_activo, m.fecha_creacion, m.area_id, a.nombre as area_nombre
    FROM menu m
    LEFT JOIN area_menu a ON m.area_id = a.area_id
    WHERE m.menu_id = ?;
"""

# Actualiza un menú. La lógica para construir SET se hará en el servicio.
# Esta es una plantilla base, necesitaremos construir la query dinámicamente.
# O una query que actualice todos los campos opcionales usando COALESCE o ISNULL.
# Ejemplo con COALESCE (SQL Server):
UPDATE_MENU_TEMPLATE = """
    UPDATE menu
    SET
        nombre = COALESCE(?, nombre),
        icono = COALESCE(?, icono),
        ruta = COALESCE(?, ruta),
        padre_menu_id = COALESCE(?, padre_menu_id),
        orden = COALESCE(?, orden),
        area_id = COALESCE(?, area_id),
        es_activo = COALESCE(?, es_activo)
    OUTPUT INSERTED.menu_id, INSERTED.nombre, INSERTED.icono, INSERTED.ruta,
           INSERTED.padre_menu_id, INSERTED.orden, INSERTED.es_activo, INSERTED.area_id,
           INSERTED.fecha_creacion -- Añadir fecha_creacion si la quieres devolver
           -- , (SELECT nombre FROM area_menu WHERE area_id = INSERTED.area_id) as area_nombre -- Subconsulta para nombre de área
    WHERE menu_id = ?;
"""
# Nota: El orden de los COALESCE debe coincidir con el orden de los parámetros opcionales en el servicio.

# Desactiva un menú (Borrado Lógico)
DEACTIVATE_MENU = """
    UPDATE menu
    SET es_activo = 0
    OUTPUT INSERTED.menu_id, INSERTED.es_activo
    WHERE menu_id = ? AND es_activo = 1;
"""

# Reactiva un menú (Opcional pero útil)
REACTIVATE_MENU = """
    UPDATE menu
    SET es_activo = 1
    OUTPUT INSERTED.menu_id, INSERTED.es_activo
    WHERE menu_id = ? AND es_activo = 0;
"""

# Verifica si un menú existe
CHECK_MENU_EXISTS = "SELECT 1 FROM menu WHERE menu_id = ?"

# Verifica si un área existe
CHECK_AREA_EXISTS = "SELECT 1 FROM area_menu WHERE area_id = ?"

# Stored Procedure para obtener todos los menús (Admin - ya definido)
GET_ALL_MENUS_ADMIN = "sp_GetAllMenuItemsAdmin" # Asegúrate que este SP devuelva area_id y area_nombre

# --- QUERIES PARA AREA_MENU (CON PAGINACIÓN Y BÚSQUEDA) ---

GET_AREAS_PAGINATED_QUERY = """
    SELECT
        area_id, nombre, descripcion, icono, es_activo, fecha_creacion
    FROM
        area_menu -- Nombre de tabla correcto
    WHERE
        (? IS NULL OR LOWER(nombre) LIKE LOWER(?) OR LOWER(descripcion) LIKE LOWER(?))
    ORDER BY
        area_id ASC
    OFFSET ? ROWS FETCH NEXT ? ROWS ONLY; -- Sintaxis SQL Server
"""

# Consulta con ROW_NUMBER() para compatibilidad (para SQL Server Native Client 10.0)
GET_AREAS_PAGINATED_QUERY_ROW_NUM = """
    SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion
    FROM (
        SELECT 
            area_id, 
            nombre, 
            descripcion, 
            icono, 
            es_activo, 
            fecha_creacion,
            ROW_NUMBER() OVER (ORDER BY area_id ASC) as row_num
        FROM area_menu
        WHERE (? IS NULL OR LOWER(nombre) LIKE LOWER(?) OR LOWER(descripcion) LIKE LOWER(?))
    ) AS numbered_rows
    WHERE row_num > ? AND row_num <= ?
    ORDER BY area_id ASC;
"""

# Consulta sin búsqueda con ROW_NUMBER()
GET_AREAS_NO_SEARCH_ROW_NUM = """
    SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion
    FROM (
        SELECT 
            area_id, 
            nombre, 
            descripcion, 
            icono, 
            es_activo, 
            fecha_creacion,
            ROW_NUMBER() OVER (ORDER BY area_id ASC) as row_num
        FROM area_menu
    ) AS numbered_rows
    WHERE row_num > ? AND row_num <= ?
    ORDER BY area_id ASC;
"""

COUNT_AREAS_QUERY = """
    SELECT
        COUNT(*) as total_count
    FROM
        area_menu -- Nombre de tabla correcto
    WHERE
        (? IS NULL OR LOWER(nombre) LIKE LOWER(?) OR LOWER(descripcion) LIKE LOWER(?));
"""

GET_AREA_BY_ID_QUERY = "SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion FROM area_menu WHERE area_id = ?;"

CHECK_AREA_EXISTS_BY_NAME_QUERY = "SELECT COUNT(*) as count FROM area_menu WHERE LOWER(nombre) = LOWER(?) AND area_id != ?;"

CREATE_AREA_QUERY = """
INSERT INTO area_menu (nombre, descripcion, icono, es_activo)
OUTPUT INSERTED.area_id, INSERTED.nombre, INSERTED.descripcion, INSERTED.icono, INSERTED.es_activo, INSERTED.fecha_creacion
VALUES (?, ?, ?, ?);
"""

UPDATE_AREA_BASE_QUERY_TEMPLATE = "UPDATE area_menu SET {fields} OUTPUT INSERTED.* WHERE area_id = ?;" # Plantilla para actualizar

TOGGLE_AREA_STATUS_QUERY = """
UPDATE area_menu SET es_activo = ?
OUTPUT INSERTED.area_id, INSERTED.nombre, INSERTED.descripcion, INSERTED.icono, INSERTED.es_activo, INSERTED.fecha_creacion
WHERE area_id = ?;
""" # Para activar/desactivar

GET_ACTIVE_AREAS_SIMPLE_LIST_QUERY = """
SELECT
    area_id,
    nombre
FROM
    area_menu -- Confirma que 'area_menu' es el nombre correcto de tu tabla
WHERE
    es_activo = 1 -- O = TRUE, dependiendo de tu SGBD
ORDER BY
    nombre ASC;
"""

GET_MENUS_BY_AREA_FOR_TREE_QUERY = """
SELECT
    m.menu_id,
    m.nombre,
    m.icono,
    m.ruta, -- Asegúrate que el nombre de columna sea 'ruta' o 'url' según tu tabla
    m.padre_menu_id,
    m.orden,
    m.es_activo,
    m.area_id,
    a.nombre as area_nombre -- Opcional: si quieres mostrar el nombre del área
    -- No incluyas 'level' aquí, build_menu_tree lo calcula si es necesario
FROM
    menu m -- Confirma el nombre de tu tabla de menú
LEFT JOIN
    area_menu a ON m.area_id = a.area_id -- Confirma nombre tabla área y JOIN
WHERE
    m.area_id = ? -- Parámetro para el ID del área
ORDER BY
    m.padre_menu_id ASC, -- Agrupa hijos bajo sus padres
    m.orden ASC; -- Ordena los hermanos entre sí
"""

# --- Queries originales que podrían quedar obsoletas ---
# GET_ALL_AREAS_ADMIN = "SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion FROM area_menu ORDER BY nombre;"
# GET_ACTIVE_AREAS = "SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion FROM area_menu WHERE es_activo = 1 ORDER BY nombre;"
# SELECT_AREA_BY_ID = "SELECT area_id, nombre, descripcion, icono, es_activo, fecha_creacion FROM area_menu WHERE area_id = ?;" # Reemplazada por GET_AREA_BY_ID_QUERY
# SELECT_AREA_BY_NAME = "SELECT area_id, nombre FROM area_menu WHERE nombre = ?;" # Reemplazada por CHECK_AREA_EXISTS_BY_NAME_QUERY
# INSERT_AREA = """...""" # Reemplazada por CREATE_AREA_QUERY
# UPDATE_AREA_TEMPLATE = "..." # Reemplazada por UPDATE_AREA_BASE_QUERY_TEMPLATE
# DEACTIVATE_AREA = "..." # Reemplazada por TOGGLE_AREA_STATUS_QUERY
# REACTIVATE_AREA = "..." # Reemplazada por TOGGLE_AREA_STATUS_QUERY

GET_MAX_ORDEN_FOR_SIBLINGS = """
    SELECT MAX(orden) as max_orden
    FROM menu
    WHERE area_id = ? AND padre_menu_id = ?;
"""

# NUEVA QUERY: Obtiene el máximo valor de 'orden' para los elementos raíz de un área
GET_MAX_ORDEN_FOR_ROOT = """
    SELECT MAX(orden) as max_orden
    FROM menu
    WHERE area_id = ? AND padre_menu_id IS NULL;
"""

# 💡 [NUEVO] QUERIES ESPECÍFICAS PARA MANTENIMIENTO DE USUARIO
# Agregamos esta query que se usa para obtener UN usuario por ID sin roles.
SELECT_USUARIO_BY_ID = """
    SELECT
        usuario_id, nombre_usuario, correo, contrasena, nombre, apellido, 
        es_activo, correo_confirmado, fecha_creacion, fecha_ultimo_acceso, 
        fecha_actualizacion, es_eliminado,
        origen_datos, codigo_trabajador_externo
    FROM
        usuario
    WHERE
        usuario_id = ? AND es_eliminado = 0;
"""

# Agregamos esta query para el INSERT de un nuevo usuario, incluyendo los campos de sincronización.
CREATE_USUARIO_QUERY = """
    INSERT INTO usuario (
        nombre_usuario, correo, contrasena, nombre, apellido, 
        es_activo, correo_confirmado, 
        origen_datos, codigo_trabajador_externo
    )
    OUTPUT INSERTED.usuario_id
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ? 
    ); 
"""

# 💡 [NUEVO] QUERY PARA ACTUALIZAR PERFIL DESDE SINCRONIZACIÓN EXTERNA (BD LOCAL)
UPDATE_USUARIO_PERFIL_EXTERNO_QUERY = """    
    UPDATE usuario SET
        nombre = ?,
        apellido = ?,
        fecha_actualizacion = GETDATE()
    OUTPUT 
        INSERTED.usuario_id, INSERTED.nombre, INSERTED.apellido, INSERTED.fecha_actualizacion
    WHERE
        usuario_id = ?
    AND
        origen_datos = 'externo';     
"""

# ⚠️ [NUEVO] QUERY CONCEPTUAL PARA DB EXTERNA DEL CLIENTE
# Esta query DEBE ser ejecutada usando la conexión dinámica del cliente.
SELECT_PERFIL_EXTERNO_QUERY = """
    SELECT 
        rtrim(dnombr) AS nombre, 
        rtrim(dappat)+' '+rtrim(dapmat) AS apellido,
        nlbele as dni_trabajador
    FROM 
        mtraba00 
    WHERE 
        ctraba = ?;
"""

# ============================================
# QUERIES PARA SISTEMA DE VACACIONES Y PERMISOS
# ============================================

# --- SOLICITUDES ---
INSERT_SOLICITUD = """
    INSERT INTO ppavac_solicitud (
        tipo_solicitud, codigo_permiso, codigo_trabajador, fecha_inicio, fecha_fin,
        hora_inicio, hora_fin, dias_solicitados, horas_solicitadas,
        observacion, motivo, estado, usuario_registro
    )
    OUTPUT INSERTED.id_solicitud, INSERTED.tipo_solicitud, INSERTED.codigo_permiso,
           INSERTED.codigo_trabajador, INSERTED.fecha_inicio, INSERTED.fecha_fin,
           INSERTED.hora_inicio, INSERTED.hora_fin,
           INSERTED.dias_solicitados, INSERTED.horas_solicitadas,
           INSERTED.observacion, INSERTED.motivo,
           INSERTED.estado, INSERTED.fecha_registro, INSERTED.usuario_registro
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'P', ?);
"""

SELECT_SOLICITUD_BY_ID = """
    SELECT 
        s.id_solicitud, s.tipo_solicitud, s.codigo_permiso, s.codigo_trabajador,
        s.fecha_inicio, s.fecha_fin, s.hora_inicio, s.hora_fin,
        s.dias_solicitados, s.horas_solicitadas, s.observacion, s.motivo,
        s.estado, s.fecha_registro, s.usuario_registro, s.fecha_modificacion,
        s.usuario_modificacion, s.fecha_anulacion, s.usuario_anulacion,
        s.motivo_anulacion, s.sregdi, s.fecha_registro_planilla,
        p.dconfa AS descripcion_permiso,
        p.ctiemp AS tiempo_catalogo
    FROM ppavac_solicitud s
    LEFT JOIN dbo.vw_mconfa00 p ON s.codigo_permiso COLLATE DATABASE_DEFAULT = p.cconfa COLLATE DATABASE_DEFAULT
    WHERE s.id_solicitud = ?;
"""

SELECT_SOLICITUDES_BY_TRABAJADOR = """
    SELECT 
        s.id_solicitud, s.tipo_solicitud, s.codigo_permiso, s.codigo_trabajador,
        s.fecha_inicio, s.fecha_fin, s.hora_inicio, s.hora_fin,
        s.dias_solicitados, s.horas_solicitadas, s.observacion, s.motivo,
        s.estado, s.fecha_registro, s.usuario_registro, s.fecha_modificacion,
        s.usuario_modificacion, s.fecha_anulacion, s.usuario_anulacion,
        s.motivo_anulacion, s.sregdi, s.fecha_registro_planilla,
        p.dconfa AS descripcion_permiso,
        p.ctiemp AS tiempo_catalogo
    FROM ppavac_solicitud s
    LEFT JOIN dbo.vw_mconfa00 p ON s.codigo_permiso COLLATE DATABASE_DEFAULT = p.cconfa COLLATE DATABASE_DEFAULT
    WHERE s.codigo_trabajador = ?
    ORDER BY s.fecha_registro DESC;
"""

SELECT_SOLICITUDES_PAGINATED = """
    WITH SolicitudesPaginadas AS (
        SELECT 
            s.id_solicitud, s.tipo_solicitud, s.codigo_permiso, s.codigo_trabajador,
            s.fecha_inicio, s.fecha_fin, s.hora_inicio, s.hora_fin,
            s.dias_solicitados, s.horas_solicitadas, s.observacion, s.motivo,
            s.estado, s.fecha_registro, s.usuario_registro, s.fecha_modificacion,
            s.usuario_modificacion, s.fecha_anulacion, s.usuario_anulacion,
            s.motivo_anulacion, s.sregdi, s.fecha_registro_planilla,
            p.dconfa AS descripcion_permiso,
            p.ctiemp AS tiempo_catalogo,
            ROW_NUMBER() OVER (ORDER BY s.fecha_registro DESC) AS rn
        FROM ppavac_solicitud s
        LEFT JOIN dbo.vw_mconfa00 p ON s.codigo_permiso COLLATE DATABASE_DEFAULT = p.cconfa COLLATE DATABASE_DEFAULT
        WHERE (? IS NULL OR s.codigo_trabajador = ?)
          AND (? IS NULL OR s.estado = ?)
          AND (? IS NULL OR s.tipo_solicitud = ?)
          AND (? IS NULL OR s.fecha_inicio >= ?)
          AND (? IS NULL OR s.fecha_fin <= ?)
    )
    SELECT *
    FROM SolicitudesPaginadas
    WHERE rn BETWEEN ? AND ?;
"""

COUNT_SOLICITUDES = """
    SELECT COUNT(*) as total
    FROM ppavac_solicitud
    WHERE (? IS NULL OR codigo_trabajador = ?)
      AND (? IS NULL OR estado = ?)
      AND (? IS NULL OR tipo_solicitud = ?)
      AND (? IS NULL OR fecha_inicio >= ?)
      AND (? IS NULL OR fecha_fin <= ?);
"""

COUNT_SOLICITUDES_SOLAPADAS = """
    SELECT COUNT(1) AS total
    FROM ppavac_solicitud s
    WHERE s.codigo_trabajador = ?
      AND s.estado IN ('P', 'A')
      AND (? IS NULL OR s.id_solicitud <> ?)
      AND (
        -- CASO 1: Bloqueo total por superposición de fechas (conflicto de días completos)
        (
          (
              ? = 0
              OR ? = 'V'
          )
          OR (s.tipo_solicitud = 'V' OR s.hora_inicio IS NULL)
        )
        AND s.fecha_inicio <= ?
        AND s.fecha_fin >= ?
      )
      OR (
        -- CASO 2: Ambos permisos por hora en el mismo día (conflicto horario)
        ? = 1
        AND ? = 'P'
        AND s.tipo_solicitud = 'P'
        AND s.hora_inicio IS NOT NULL
        AND s.hora_fin IS NOT NULL
        AND s.fecha_inicio <= ?
        AND s.fecha_fin >= ?
        AND s.hora_inicio <= ?
        AND s.hora_fin >= ?
      );
"""

SELECT_TIPO_PERMISO_BY_CODIGO = """
    SELECT ctiemp AS tiempo
    FROM dbo.vw_mconfa00
    WHERE cconfa = ?;
"""

UPDATE_SOLICITUD = """
    UPDATE ppavac_solicitud
    SET 
        fecha_inicio = COALESCE(?, fecha_inicio),
        fecha_fin = COALESCE(?, fecha_fin),
        hora_inicio = COALESCE(?, hora_inicio),
        hora_fin = COALESCE(?, hora_fin),
        dias_solicitados = COALESCE(?, dias_solicitados),
        horas_solicitadas = COALESCE(?, horas_solicitadas),
        observacion = COALESCE(?, observacion),
        motivo = COALESCE(?, motivo),
        fecha_modificacion = GETDATE(),
        usuario_modificacion = ?
    OUTPUT INSERTED.id_solicitud, INSERTED.tipo_solicitud, INSERTED.codigo_permiso,
           INSERTED.codigo_trabajador, INSERTED.fecha_inicio, INSERTED.fecha_fin,
           INSERTED.hora_inicio, INSERTED.hora_fin,
           INSERTED.dias_solicitados, INSERTED.horas_solicitadas,
           INSERTED.observacion, INSERTED.motivo,
           INSERTED.estado, INSERTED.fecha_registro, INSERTED.usuario_registro,
           INSERTED.fecha_modificacion, INSERTED.usuario_modificacion
    WHERE id_solicitud = ? AND estado = 'P';
"""

ANULAR_SOLICITUD = """
    UPDATE ppavac_solicitud
    SET 
        estado = 'N',
        fecha_anulacion = GETDATE(),
        usuario_anulacion = ?,
        motivo_anulacion = ?
    OUTPUT INSERTED.id_solicitud, INSERTED.estado, INSERTED.fecha_anulacion,
           INSERTED.usuario_anulacion, INSERTED.motivo_anulacion
    WHERE id_solicitud = ? AND estado = 'P';
"""

# --- APROBACIONES ---
INSERT_APROBACION = """
    INSERT INTO ppavac_aprobacion (
        id_solicitud, nivel, codigo_trabajador_aprueba, estado, observacion,
        fecha, usuario, ip_dispositivo, fecha_notificado
    )
    OUTPUT INSERTED.id_aprobacion, INSERTED.id_solicitud, INSERTED.nivel,
           INSERTED.codigo_trabajador_aprueba, INSERTED.estado, INSERTED.observacion,
           INSERTED.fecha, INSERTED.usuario, INSERTED.ip_dispositivo, INSERTED.fecha_notificado
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

SELECT_APROBACIONES_BY_SOLICITUD = """
    SELECT 
        id_aprobacion, id_solicitud, nivel, codigo_trabajador_aprueba,
        estado, observacion, fecha, usuario, ip_dispositivo, fecha_notificado
    FROM ppavac_aprobacion
    WHERE id_solicitud = ?
    ORDER BY nivel ASC;
"""

SELECT_APROBACIONES_PENDIENTES = """
    SELECT 
        a.id_aprobacion, a.id_solicitud, a.nivel, a.codigo_trabajador_aprueba,
        a.estado, a.observacion, a.fecha, a.usuario, a.ip_dispositivo, a.fecha_notificado,
        s.tipo_solicitud, s.codigo_trabajador, s.fecha_inicio, s.fecha_fin, s.dias_solicitados,
        LTRIM(RTRIM(ISNULL(t.dtraba, ''))) AS nombre_trabajador
    FROM ppavac_aprobacion a
    INNER JOIN ppavac_solicitud s ON a.id_solicitud = s.id_solicitud
    LEFT JOIN dbo.vw_mtraba10 t ON t.ctraba COLLATE DATABASE_DEFAULT = s.codigo_trabajador COLLATE DATABASE_DEFAULT
    WHERE a.codigo_trabajador_aprueba = ?
      AND a.estado = 'P'
      AND s.estado = 'P'
      -- Verificar que todos los niveles anteriores ya fueron aprobados
      AND NOT EXISTS (
          SELECT 1 
          FROM ppavac_aprobacion a_prev
          WHERE a_prev.id_solicitud = a.id_solicitud
            AND a_prev.nivel < a.nivel
            AND a_prev.estado != 'A'
      )
    ORDER BY a.fecha_notificado ASC, s.fecha_registro ASC;
"""

UPDATE_APROBACION = """
    UPDATE ppavac_aprobacion
    SET 
        estado = ?,
        observacion = COALESCE(?, observacion),
        fecha = GETDATE(),
        usuario = ?,
        ip_dispositivo = COALESCE(?, ip_dispositivo)
    OUTPUT INSERTED.id_aprobacion, INSERTED.id_solicitud, INSERTED.nivel,
           INSERTED.codigo_trabajador_aprueba, INSERTED.estado, INSERTED.observacion,
           INSERTED.fecha, INSERTED.usuario, INSERTED.ip_dispositivo
    WHERE id_aprobacion = ? AND estado = 'P';
"""

UPDATE_SOLICITUD_ESTADO = """
    UPDATE ppavac_solicitud
    SET 
        estado = ?,
        fecha_modificacion = GETDATE(),
        usuario_modificacion = ?
    OUTPUT INSERTED.id_solicitud, INSERTED.estado
    WHERE id_solicitud = ?;
"""

# --- NOTIFICACIONES ---
INSERT_NOTIFICACION = """
    INSERT INTO ppavac_notificacion (
        codigo_trabajador, id_solicitud, tipo_notificacion, titulo, mensaje,
        leido, accion_url, prioridad
    )
    OUTPUT INSERTED.id_notificacion, INSERTED.codigo_trabajador, INSERTED.id_solicitud,
           INSERTED.tipo_notificacion, INSERTED.titulo, INSERTED.mensaje,
           INSERTED.leido, INSERTED.fecha_creacion, INSERTED.fecha_leido,
           INSERTED.accion_url, INSERTED.prioridad
    VALUES (?, ?, ?, ?, ?, 'N', ?, ?);
"""

SELECT_NOTIFICACIONES_BY_TRABAJADOR = """
    SELECT 
        id_notificacion, codigo_trabajador, id_solicitud, tipo_notificacion,
        titulo, mensaje, leido, fecha_creacion, fecha_leido, accion_url, prioridad
    FROM ppavac_notificacion
    WHERE codigo_trabajador = ?
    ORDER BY fecha_creacion DESC;
"""

SELECT_NOTIFICACIONES_PAGINATED = """
    WITH NotificacionesPaginadas AS (
        SELECT 
            id_notificacion, codigo_trabajador, id_solicitud, tipo_notificacion,
            titulo, mensaje, leido, fecha_creacion, fecha_leido, accion_url, prioridad,
            ROW_NUMBER() OVER (ORDER BY fecha_creacion DESC) AS rn
        FROM ppavac_notificacion
        WHERE codigo_trabajador = ?
          AND (? IS NULL OR leido = ?)
    )
    SELECT *
    FROM NotificacionesPaginadas
    WHERE rn BETWEEN ? AND ?;
"""

COUNT_NOTIFICACIONES = """
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN leido = 'N' THEN 1 ELSE 0 END) as no_leidas
    FROM ppavac_notificacion
    WHERE codigo_trabajador = ?;
"""

UPDATE_NOTIFICACION_LEIDA = """
    UPDATE ppavac_notificacion
    SET 
        leido = 'S',
        fecha_leido = GETDATE()
    OUTPUT INSERTED.id_notificacion, INSERTED.leido, INSERTED.fecha_leido
    WHERE id_notificacion = ? AND leido = 'N';
"""

# --- CONFIGURACIÓN DE FLUJO ---
SELECT_CONFIG_FLUJO_APLICABLE = """
    SELECT TOP 1
        id_config, tipo_solicitud, codigo_permiso, codigo_area, codigo_seccion,
        codigo_cargo, dias_desde, dias_hasta, niveles_requeridos, orden, activo
    FROM ppavac_config_flujo
    WHERE tipo_solicitud = ?
      AND (? IS NULL OR codigo_permiso IS NULL OR codigo_permiso = ?)
      AND (? IS NULL OR codigo_area IS NULL OR codigo_area = ?)
      AND (? IS NULL OR codigo_seccion IS NULL OR codigo_seccion = ?)
      AND (? IS NULL OR codigo_cargo IS NULL OR codigo_cargo = ?)
      AND (? IS NULL OR dias_desde IS NULL OR ? >= dias_desde)
      AND (? IS NULL OR dias_hasta IS NULL OR ? <= dias_hasta)
      AND activo = 'S'
      AND (fecha_hasta IS NULL OR fecha_hasta >= GETDATE())
    ORDER BY orden ASC;
"""

SELECT_ALL_CONFIG_FLUJO = """
    SELECT 
        id_config, tipo_solicitud, codigo_permiso, codigo_area, codigo_seccion,
        codigo_cargo, dias_desde, dias_hasta, niveles_requeridos, orden, activo,
        fecha_desde, fecha_hasta, usuario_registro, fecha_registro, descripcion
    FROM ppavac_config_flujo
    ORDER BY orden ASC, fecha_registro DESC;
"""

# --- JERARQUÍA ---
SELECT_JERARQUIA_APROBADORES = """
    SELECT 
        id_jerarquia, codigo_area, codigo_seccion, codigo_cargo,
        codigo_trabajador_aprobador, tipo_relacion, nivel_jerarquico, activo
    FROM ppavac_jerarquia
    WHERE (? IS NULL OR codigo_area IS NULL OR codigo_area = ?)
      AND (? IS NULL OR codigo_seccion IS NULL OR codigo_seccion = ?)
      AND (? IS NULL OR codigo_cargo IS NULL OR codigo_cargo = ?)
      AND activo = 'S'
      AND (fecha_hasta IS NULL OR fecha_hasta >= GETDATE())
    ORDER BY nivel_jerarquico ASC;
"""

SELECT_ALL_JERARQUIA = """
    SELECT 
        id_jerarquia, codigo_area, codigo_seccion, codigo_cargo,
        codigo_trabajador_aprobador, tipo_relacion, nivel_jerarquico, activo,
        fecha_desde, fecha_hasta, usuario_registro, fecha_registro, descripcion
    FROM ppavac_jerarquia
    ORDER BY nivel_jerarquico ASC, fecha_registro DESC;
"""

SELECT_ES_APROBADOR = """
    SELECT 
        COUNT(*) AS total_configuraciones,
        MIN(nivel_jerarquico) AS nivel_minimo,
        MAX(nivel_jerarquico) AS nivel_maximo
    FROM ppavac_jerarquia
    WHERE codigo_trabajador_aprobador = ?
      AND activo = 'S'
      AND (fecha_hasta IS NULL OR fecha_hasta >= GETDATE());
"""

SELECT_NIVELES_APROBADOR = """
    SELECT nivel_jerarquico
    FROM ppavac_jerarquia
    WHERE codigo_trabajador_aprobador = ?
      AND activo = 'S'
      AND (fecha_hasta IS NULL OR fecha_hasta >= GETDATE())
    ORDER BY nivel_jerarquico ASC;
"""

# --- SUSTITUTOS ---
SELECT_SUSTITUTO_ACTIVO = """
    SELECT TOP 1
        id_sustituto, codigo_trabajador_titular, codigo_trabajador_sustituto,
        fecha_desde, fecha_hasta, motivo, observacion, activo
    FROM ppavac_sustituto
    WHERE codigo_trabajador_titular = ?
      AND activo = 'S'
      AND fecha_desde <= GETDATE()
      AND fecha_hasta >= GETDATE()
    ORDER BY fecha_registro DESC;
"""

SELECT_ALL_SUSTITUTOS = """
    SELECT 
        id_sustituto, codigo_trabajador_titular, codigo_trabajador_sustituto,
        fecha_desde, fecha_hasta, motivo, observacion, activo,
        usuario_registro, fecha_registro
    FROM ppavac_sustituto
    ORDER BY fecha_registro DESC;
"""

# --- DISPOSITIVOS ---
INSERT_DISPOSITIVO = """
    INSERT INTO ppavac_dispositivo (
        codigo_trabajador, token_fcm, plataforma, modelo_dispositivo,
        version_app, version_so, activo, notif_nuevas, notif_aprobadas, notif_rechazadas
    )
    OUTPUT INSERTED.id_dispositivo, INSERTED.codigo_trabajador, INSERTED.token_fcm,
           INSERTED.plataforma, INSERTED.modelo_dispositivo, INSERTED.version_app,
           INSERTED.version_so, INSERTED.fecha_registro, INSERTED.activo,
           INSERTED.notif_nuevas, INSERTED.notif_aprobadas, INSERTED.notif_rechazadas
    VALUES (?, ?, ?, ?, ?, ?, 'S', 'S', 'S', 'S');
"""

SELECT_DISPOSITIVOS_BY_TRABAJADOR = """
    SELECT 
        id_dispositivo, codigo_trabajador, token_fcm, plataforma, modelo_dispositivo,
        version_app, version_so, fecha_registro, fecha_ultimo_acceso, activo,
        notif_nuevas, notif_aprobadas, notif_rechazadas
    FROM ppavac_dispositivo
    WHERE codigo_trabajador = ? AND activo = 'S';
"""

UPDATE_DISPOSITIVO_TOKEN = """
    UPDATE ppavac_dispositivo
    SET 
        token_fcm = ?,
        fecha_ultimo_acceso = GETDATE(),
        modelo_dispositivo = COALESCE(?, modelo_dispositivo),
        version_app = COALESCE(?, version_app),
        version_so = COALESCE(?, version_so)
    OUTPUT INSERTED.id_dispositivo, INSERTED.token_fcm, INSERTED.fecha_ultimo_acceso
    WHERE codigo_trabajador = ? AND token_fcm = ?;
"""

# --- SALDOS DE VACACIONES ---
SELECT_SALDO_VACACIONES = """
    SELECT 
        t.ctraba AS codigo_trabajador,
        CAST(30.00 AS DECIMAL(5,2)) AS dias_asignados_totales,
        CAST(COALESCE(SUM(CASE WHEN s.estado = 'A' AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS dias_usados,
        CAST(COALESCE(SUM(CASE WHEN s.estado = 'P' AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS dias_pendientes,
        CAST(30.00 - COALESCE(SUM(CASE WHEN s.estado IN ('A', 'P') AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS saldo_disponible
    FROM dbo.vw_mtraba10 t
    LEFT JOIN ppavac_solicitud s ON t.ctraba COLLATE DATABASE_DEFAULT = s.codigo_trabajador COLLATE DATABASE_DEFAULT
        AND s.tipo_solicitud = 'V' 
        AND s.estado != 'N'
    WHERE t.ctraba COLLATE DATABASE_DEFAULT = ?
    GROUP BY t.ctraba;
"""

# --- CATÁLOGOS ---
SELECT_CATALOGO_AREAS = """
    SELECT careas AS codigo, dareas AS descripcion
    FROM dbo.vw_tareas00
    ORDER BY dareas;
"""

SELECT_CATALOGO_SECCIONES = """
    SELECT csecci AS codigo, dsecci AS descripcion
    FROM dbo.vw_tsecci00
    ORDER BY dsecci;
"""

SELECT_CATALOGO_CARGOS = """
    SELECT ccargo AS codigo, dcargo AS descripcion
    FROM dbo.vw_tcargo00
    ORDER BY dcargo;
"""

SELECT_CATALOGO_TIPOS_PERMISO = """
    SELECT cconfa AS codigo, dconfa AS descripcion, ctiemp AS tiempo
    FROM dbo.vw_mconfa00
    ORDER BY dconfa;
"""

# --- TRABAJADORES Y CUMPLEAÑOS ---
SELECT_CUMPLEANOS_HOY = """
    WITH UnTrabajadorPorFila AS (
        SELECT 
            t.ctraba,
            LTRIM(RTRIM(ISNULL(t.dtraba, ''))) AS nombre_completo,
            t.careas AS codigo_area,
            t.csecci AS codigo_seccion,
            t.ccargo AS codigo_cargo,
            RTRIM(ISNULL(a.dareas, '')) AS descripcion_area,
            RTRIM(ISNULL(s.dsecci, '')) AS descripcion_seccion,
            RTRIM(ISNULL(c.dcargo, '')) AS descripcion_cargo,
            t.numdni AS dni,
            CAST(t.fnacim AS DATE) AS fecha_nacimiento,
            CAST(t.fingre AS DATE) AS fecha_ingreso,
            CAST(t.ffinco AS DATE) AS fecha_fin_contrato,
            t.nemail01 AS correo,
            t.ntftra AS telefono,
            ROW_NUMBER() OVER (PARTITION BY t.ctraba ORDER BY t.csecci, t.ccargo) AS rn_worker
        FROM dbo.vw_mtraba10 t
        LEFT JOIN dbo.vw_tareas00 a ON t.careas COLLATE DATABASE_DEFAULT = a.careas COLLATE DATABASE_DEFAULT
        LEFT JOIN dbo.vw_tsecci00 s ON t.csecci COLLATE DATABASE_DEFAULT = s.csecci COLLATE DATABASE_DEFAULT
        LEFT JOIN dbo.vw_tcargo00 c ON t.ccargo COLLATE DATABASE_DEFAULT = c.ccargo COLLATE DATABASE_DEFAULT
        WHERE (t.svigen = 'S' AND t.straba = 'A')
          AND DAY(t.fnacim) = DAY(GETDATE())
          AND MONTH(t.fnacim) = MONTH(GETDATE())
    ),
    CumpleanosPaginados AS (
        SELECT 
            ctraba AS codigo_trabajador,
            nombre_completo,
            codigo_area,
            codigo_seccion,
            codigo_cargo,
            descripcion_area,
            descripcion_seccion,
            descripcion_cargo,
            dni,
            fecha_nacimiento,
            fecha_ingreso,
            fecha_fin_contrato,
            correo,
            telefono,
            ROW_NUMBER() OVER (ORDER BY nombre_completo, ctraba) AS rn
        FROM UnTrabajadorPorFila
        WHERE rn_worker = 1
    )
    SELECT 
        codigo_trabajador,
        nombre_completo,
        codigo_area,
        codigo_seccion,
        codigo_cargo,
        descripcion_area,
        descripcion_seccion,
        descripcion_cargo,
        dni,
        fecha_nacimiento,
        fecha_ingreso,
        fecha_fin_contrato,
        correo,
        telefono
    FROM CumpleanosPaginados
    WHERE rn > ? AND rn <= ?;
"""

COUNT_CUMPLEANOS_HOY = """
    SELECT COUNT(DISTINCT t.ctraba) AS total
    FROM dbo.vw_mtraba10 t
    WHERE (t.svigen = 'S' AND t.straba = 'A')
      AND DAY(t.fnacim) = DAY(GETDATE())
      AND MONTH(t.fnacim) = MONTH(GETDATE());
"""

SELECT_TRABAJADORES_PAGINATED = """
    WITH UnTrabajadorPorFila AS (
        SELECT 
            t.ctraba,
            LTRIM(RTRIM(ISNULL(t.dtraba, ''))) AS nombre_completo,
            t.careas AS codigo_area,
            t.csecci AS codigo_seccion,
            t.ccargo AS codigo_cargo,
            RTRIM(ISNULL(a.dareas, '')) AS descripcion_area,
            RTRIM(ISNULL(s.dsecci, '')) AS descripcion_seccion,
            RTRIM(ISNULL(c.dcargo, '')) AS descripcion_cargo,
            t.numdni AS dni,
            CAST(t.fnacim AS DATE) AS fecha_nacimiento,
            CAST(t.fingre AS DATE) AS fecha_ingreso,
            CAST(t.ffinco AS DATE) AS fecha_fin_contrato,
            t.nemail01 AS correo,
            t.ntftra AS telefono,
            ROW_NUMBER() OVER (PARTITION BY t.ctraba ORDER BY t.csecci, t.ccargo) AS rn_worker
        FROM dbo.vw_mtraba10 t
        LEFT JOIN dbo.vw_tareas00 a ON t.careas COLLATE DATABASE_DEFAULT = a.careas COLLATE DATABASE_DEFAULT
        LEFT JOIN dbo.vw_tsecci00 s ON t.csecci COLLATE DATABASE_DEFAULT = s.csecci COLLATE DATABASE_DEFAULT
        LEFT JOIN dbo.vw_tcargo00 c ON t.ccargo COLLATE DATABASE_DEFAULT = c.ccargo COLLATE DATABASE_DEFAULT
        WHERE (t.svigen = 'S' AND t.straba = 'A')
          AND (? IS NULL OR LOWER(t.ctraba) LIKE LOWER(?))
          AND (? IS NULL OR LOWER(LTRIM(RTRIM(ISNULL(t.dtraba, '')))) LIKE LOWER(?))
          AND (? IS NULL OR t.careas = ?)
          AND (? IS NULL OR t.csecci = ?)
          AND (? IS NULL OR t.ccargo = ?)
    ),
    TrabajadoresPaginados AS (
        SELECT 
            ctraba AS codigo_trabajador,
            nombre_completo,
            codigo_area,
            codigo_seccion,
            codigo_cargo,
            descripcion_area,
            descripcion_seccion,
            descripcion_cargo,
            dni,
            fecha_nacimiento,
            fecha_ingreso,
            fecha_fin_contrato,
            correo,
            telefono,
            ROW_NUMBER() OVER (ORDER BY nombre_completo, ctraba) AS rn
        FROM UnTrabajadorPorFila
        WHERE rn_worker = 1
    )
    SELECT 
        codigo_trabajador,
        nombre_completo,
        codigo_area,
        codigo_seccion,
        codigo_cargo,
        descripcion_area,
        descripcion_seccion,
        descripcion_cargo,
        dni,
        fecha_nacimiento,
        fecha_ingreso,
        fecha_fin_contrato,
        correo,
        telefono
    FROM TrabajadoresPaginados
    WHERE rn > ? AND rn <= ?;
"""

COUNT_TRABAJADORES = """
    SELECT COUNT(DISTINCT t.ctraba) AS total
    FROM dbo.vw_mtraba10 t
    WHERE (t.svigen = 'S' AND t.straba = 'A')
      AND (? IS NULL OR LOWER(t.ctraba) LIKE LOWER(?))
      AND (? IS NULL OR LOWER(LTRIM(RTRIM(ISNULL(t.dtraba, '')))) LIKE LOWER(?))
      AND (? IS NULL OR t.careas = ?)
      AND (? IS NULL OR t.csecci = ?)
      AND (? IS NULL OR t.ccargo = ?);
"""

# --- BÚSQUEDAS DE CATÁLOGOS CON FILTROS ---
# Compatible con SQL Server 2008+ usando ROW_NUMBER()
SELECT_BUSCAR_AREAS = """
    SELECT careas AS codigo, dareas AS descripcion
    FROM (
        SELECT 
            careas, dareas,
            ROW_NUMBER() OVER (ORDER BY dareas) AS rn
        FROM dbo.vw_tareas00
        WHERE (? IS NULL OR LOWER(careas) LIKE LOWER(?))
          AND (? IS NULL OR LOWER(dareas) LIKE LOWER(?))
    ) AS numbered_rows
    WHERE rn > ? AND rn <= ?;
"""

COUNT_BUSCAR_AREAS = """
    SELECT COUNT(*) AS total
    FROM dbo.vw_tareas00
    WHERE (? IS NULL OR LOWER(careas) LIKE LOWER(?))
      AND (? IS NULL OR LOWER(dareas) LIKE LOWER(?));
"""

SELECT_BUSCAR_SECCIONES = """
    SELECT csecci AS codigo, dsecci AS descripcion
    FROM (
        SELECT 
            csecci, dsecci,
            ROW_NUMBER() OVER (ORDER BY dsecci) AS rn
        FROM dbo.vw_tsecci00
        WHERE (? IS NULL OR LOWER(csecci) LIKE LOWER(?))
          AND (? IS NULL OR LOWER(dsecci) LIKE LOWER(?))
    ) AS numbered_rows
    WHERE rn > ? AND rn <= ?;
"""

COUNT_BUSCAR_SECCIONES = """
    SELECT COUNT(*) AS total
    FROM dbo.vw_tsecci00
    WHERE (? IS NULL OR LOWER(csecci) LIKE LOWER(?))
      AND (? IS NULL OR LOWER(dsecci) LIKE LOWER(?));
"""

SELECT_BUSCAR_CARGOS = """
    SELECT ccargo AS codigo, dcargo AS descripcion
    FROM (
        SELECT 
            ccargo, dcargo,
            ROW_NUMBER() OVER (ORDER BY dcargo) AS rn
        FROM dbo.vw_tcargo00
        WHERE (? IS NULL OR LOWER(ccargo) LIKE LOWER(?))
          AND (? IS NULL OR LOWER(dcargo) LIKE LOWER(?))
    ) AS numbered_rows
    WHERE rn > ? AND rn <= ?;
"""

COUNT_BUSCAR_CARGOS = """
    SELECT COUNT(*) AS total
    FROM dbo.vw_tcargo00
    WHERE (? IS NULL OR LOWER(ccargo) LIKE LOWER(?))
      AND (? IS NULL OR LOWER(dcargo) LIKE LOWER(?));
"""

SELECT_BUSCAR_TRABAJADORES = """
    WITH UnTrabajadorPorFila AS (
        SELECT 
            ctraba AS codigo,
            LTRIM(RTRIM(ISNULL(dtraba, ''))) AS nombre_completo,
            careas AS codigo_area,
            csecci AS codigo_seccion,
            ccargo AS codigo_cargo,
            numdni AS numero_dni,
            ROW_NUMBER() OVER (PARTITION BY ctraba ORDER BY csecci, ccargo) AS rn_worker
        FROM dbo.vw_mtraba10
        WHERE (svigen = 'S' AND straba = 'A')
          AND (? IS NULL OR LOWER(ctraba) LIKE LOWER(?))
          AND (? IS NULL OR LOWER(LTRIM(RTRIM(ISNULL(dtraba, '')))) LIKE LOWER(?))
          AND (? IS NULL OR careas = ?)
          AND (? IS NULL OR csecci = ?)
          AND (? IS NULL OR ccargo = ?)
          AND (? IS NULL OR numdni LIKE ?)
    ),
    TrabajadoresNumerados AS (
        SELECT codigo, nombre_completo, codigo_area, codigo_seccion, codigo_cargo, numero_dni,
               ROW_NUMBER() OVER (ORDER BY nombre_completo, codigo) AS rn
        FROM UnTrabajadorPorFila
        WHERE rn_worker = 1
    )
    SELECT codigo, nombre_completo, codigo_area, codigo_seccion, codigo_cargo, numero_dni
    FROM TrabajadoresNumerados
    WHERE rn > ? AND rn <= ?;
"""

COUNT_BUSCAR_TRABAJADORES = """
    SELECT COUNT(DISTINCT ctraba) AS total
    FROM dbo.vw_mtraba10
    WHERE (svigen = 'S' AND straba = 'A')
      AND (? IS NULL OR LOWER(ctraba) LIKE LOWER(?))
      AND (? IS NULL OR LOWER(LTRIM(RTRIM(ISNULL(dtraba, '')))) LIKE LOWER(?))
      AND (? IS NULL OR careas = ?)
      AND (? IS NULL OR csecci = ?)
      AND (? IS NULL OR ccargo = ?)
      AND (? IS NULL OR numdni LIKE ?);
"""

SELECT_TRABAJADOR_BY_CODIGO = """
    SELECT 
        ctraba AS codigo_trabajador,
        dtraba AS nombre_completo,
        careas AS codigo_area,
        csecci AS codigo_seccion,
        ccargo AS codigo_cargo,
        numdni AS dni
    FROM dbo.vw_mtraba10
    WHERE ctraba = ?;
"""

# --- QUERIES ADMINISTRATIVAS ---
INSERT_CONFIG_FLUJO = """
    INSERT INTO ppavac_config_flujo (
        tipo_solicitud, codigo_permiso, codigo_area, codigo_seccion, codigo_cargo,
        dias_desde, dias_hasta, niveles_requeridos, orden, activo,
        fecha_desde, fecha_hasta, usuario_registro, descripcion
    )
    OUTPUT INSERTED.id_config, INSERTED.tipo_solicitud, INSERTED.codigo_permiso,
           INSERTED.codigo_area, INSERTED.codigo_seccion, INSERTED.codigo_cargo,
           INSERTED.dias_desde, INSERTED.dias_hasta, INSERTED.niveles_requeridos,
           INSERTED.orden, INSERTED.activo, INSERTED.fecha_desde, INSERTED.fecha_hasta,
           INSERTED.usuario_registro, INSERTED.fecha_registro, INSERTED.descripcion
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

UPDATE_CONFIG_FLUJO = """
    UPDATE ppavac_config_flujo
    SET 
        tipo_solicitud = COALESCE(?, tipo_solicitud),
        codigo_permiso = ?,
        codigo_area = ?,
        codigo_seccion = ?,
        codigo_cargo = ?,
        dias_desde = ?,
        dias_hasta = ?,
        niveles_requeridos = COALESCE(?, niveles_requeridos),
        orden = COALESCE(?, orden),
        activo = COALESCE(?, activo),
        fecha_desde = COALESCE(?, fecha_desde),
        fecha_hasta = ?,
        descripcion = COALESCE(?, descripcion)
    OUTPUT INSERTED.id_config, INSERTED.tipo_solicitud, INSERTED.codigo_permiso,
           INSERTED.codigo_area, INSERTED.codigo_seccion, INSERTED.codigo_cargo,
           INSERTED.dias_desde, INSERTED.dias_hasta, INSERTED.niveles_requeridos,
           INSERTED.orden, INSERTED.activo, INSERTED.fecha_desde, INSERTED.fecha_hasta,
           INSERTED.usuario_registro, INSERTED.fecha_registro, INSERTED.descripcion
    WHERE id_config = ?;
"""

DELETE_CONFIG_FLUJO = """
    DELETE FROM ppavac_config_flujo
    WHERE id_config = ?;
"""

SELECT_CONFIG_FLUJO_BY_ID = """
    SELECT 
        id_config, tipo_solicitud, codigo_permiso, codigo_area, codigo_seccion,
        codigo_cargo, dias_desde, dias_hasta, niveles_requeridos, orden, activo,
        fecha_desde, fecha_hasta, usuario_registro, fecha_registro, descripcion
    FROM ppavac_config_flujo
    WHERE id_config = ?;
"""

INSERT_JERARQUIA = """
    INSERT INTO ppavac_jerarquia (
        codigo_area, codigo_seccion, codigo_cargo, codigo_trabajador_aprobador,
        tipo_relacion, nivel_jerarquico, activo, fecha_desde, fecha_hasta,
        usuario_registro, descripcion
    )
    OUTPUT INSERTED.id_jerarquia, INSERTED.codigo_area, INSERTED.codigo_seccion,
           INSERTED.codigo_cargo, INSERTED.codigo_trabajador_aprobador,
           INSERTED.tipo_relacion, INSERTED.nivel_jerarquico, INSERTED.activo,
           INSERTED.fecha_desde, INSERTED.fecha_hasta, INSERTED.usuario_registro,
           INSERTED.fecha_registro, INSERTED.descripcion
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

UPDATE_JERARQUIA = """
    UPDATE ppavac_jerarquia
    SET 
        codigo_area = ?,
        codigo_seccion = ?,
        codigo_cargo = ?,
        codigo_trabajador_aprobador = COALESCE(?, codigo_trabajador_aprobador),
        tipo_relacion = COALESCE(?, tipo_relacion),
        nivel_jerarquico = COALESCE(?, nivel_jerarquico),
        activo = COALESCE(?, activo),
        fecha_desde = COALESCE(?, fecha_desde),
        fecha_hasta = ?,
        descripcion = COALESCE(?, descripcion)
    OUTPUT INSERTED.id_jerarquia, INSERTED.codigo_area, INSERTED.codigo_seccion,
           INSERTED.codigo_cargo, INSERTED.codigo_trabajador_aprobador,
           INSERTED.tipo_relacion, INSERTED.nivel_jerarquico, INSERTED.activo,
           INSERTED.fecha_desde, INSERTED.fecha_hasta, INSERTED.usuario_registro,
           INSERTED.fecha_registro, INSERTED.descripcion
    WHERE id_jerarquia = ?;
"""

SELECT_JERARQUIA_BY_ID = """
    SELECT 
        id_jerarquia, codigo_area, codigo_seccion, codigo_cargo,
        codigo_trabajador_aprobador, tipo_relacion, nivel_jerarquico, activo,
        fecha_desde, fecha_hasta, usuario_registro, fecha_registro, descripcion
    FROM ppavac_jerarquia
    WHERE id_jerarquia = ?;
"""

DELETE_JERARQUIA = """
    DELETE FROM ppavac_jerarquia
    WHERE id_jerarquia = ?;
"""

INSERT_SUSTITUTO = """
    INSERT INTO ppavac_sustituto (
        codigo_trabajador_titular, codigo_trabajador_sustituto,
        fecha_desde, fecha_hasta, motivo, observacion, activo, usuario_registro
    )
    OUTPUT INSERTED.id_sustituto, INSERTED.codigo_trabajador_titular,
           INSERTED.codigo_trabajador_sustituto, INSERTED.fecha_desde,
           INSERTED.fecha_hasta, INSERTED.motivo, INSERTED.observacion,
           INSERTED.activo, INSERTED.usuario_registro, INSERTED.fecha_registro
    VALUES (?, ?, ?, ?, ?, ?, 'S', ?);
"""

UPDATE_SUSTITUTO = """
    UPDATE ppavac_sustituto
    SET 
        codigo_trabajador_titular = COALESCE(?, codigo_trabajador_titular),
        codigo_trabajador_sustituto = COALESCE(?, codigo_trabajador_sustituto),
        fecha_desde = COALESCE(?, fecha_desde),
        fecha_hasta = COALESCE(?, fecha_hasta),
        motivo = COALESCE(?, motivo),
        observacion = COALESCE(?, observacion),
        activo = COALESCE(?, activo)
    OUTPUT INSERTED.id_sustituto, INSERTED.codigo_trabajador_titular,
           INSERTED.codigo_trabajador_sustituto, INSERTED.fecha_desde,
           INSERTED.fecha_hasta, INSERTED.motivo, INSERTED.observacion,
           INSERTED.activo, INSERTED.usuario_registro, INSERTED.fecha_registro
    WHERE id_sustituto = ?;
"""

SELECT_SUSTITUTO_BY_ID = """
    SELECT 
        id_sustituto, codigo_trabajador_titular, codigo_trabajador_sustituto,
        fecha_desde, fecha_hasta, motivo, observacion, activo,
        usuario_registro, fecha_registro
    FROM ppavac_sustituto
    WHERE id_sustituto = ?;
"""

SELECT_ESTADISTICAS_SOLICITUDES = """
    SELECT 
        COUNT(*) AS total_solicitudes,
        SUM(CASE WHEN estado = 'P' THEN 1 ELSE 0 END) AS solicitudes_pendientes,
        SUM(CASE WHEN estado = 'A' THEN 1 ELSE 0 END) AS solicitudes_aprobadas,
        SUM(CASE WHEN estado = 'R' THEN 1 ELSE 0 END) AS solicitudes_rechazadas,
        SUM(CASE WHEN tipo_solicitud = 'V' THEN 1 ELSE 0 END) AS solicitudes_vacaciones,
        SUM(CASE WHEN tipo_solicitud = 'P' THEN 1 ELSE 0 END) AS solicitudes_permisos
    FROM ppavac_solicitud
    WHERE (? IS NULL OR fecha_registro >= ?)
      AND (? IS NULL OR fecha_registro <= ?);
"""

SELECT_SOLICITUDES_POR_MES = """
    SELECT 
        YEAR(fecha_registro) AS año,
        MONTH(fecha_registro) AS mes,
        COUNT(*) AS cantidad,
        SUM(CASE WHEN tipo_solicitud = 'V' THEN 1 ELSE 0 END) AS vacaciones,
        SUM(CASE WHEN tipo_solicitud = 'P' THEN 1 ELSE 0 END) AS permisos
    FROM ppavac_solicitud
    WHERE (? IS NULL OR fecha_registro >= ?)
      AND (? IS NULL OR fecha_registro <= ?)
    GROUP BY YEAR(fecha_registro), MONTH(fecha_registro)
    ORDER BY año DESC, mes DESC;
"""

SELECT_ALL_SALDOS_VACACIONES = """
    SELECT 
        t.ctraba AS codigo_trabajador,
        t.dtraba AS nombre_trabajador,
        CAST(30.00 AS DECIMAL(5,2)) AS dias_asignados_totales,
        CAST(COALESCE(SUM(CASE WHEN s.estado = 'A' AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS dias_usados,
        CAST(COALESCE(SUM(CASE WHEN s.estado = 'P' AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS dias_pendientes,
        CAST(30.00 - COALESCE(SUM(CASE WHEN s.estado IN ('A', 'P') AND s.tipo_solicitud = 'V' THEN s.dias_solicitados ELSE 0 END), 0) AS DECIMAL(5,2)) AS saldo_disponible
    FROM dbo.vw_mtraba10 t
    LEFT JOIN ppavac_solicitud s ON t.ctraba = s.codigo_trabajador AND s.estado != 'N'
    WHERE (? IS NULL OR t.careas = ?)
      AND (? IS NULL OR t.csecci = ?)
    GROUP BY t.ctraba, t.dtraba
    ORDER BY t.dtraba;
"""

# --- DISPOSITIVOS Y NOTIFICACIONES ---
SELECT_DISPOSITIVO_BY_TOKEN = """
    SELECT 
        id_dispositivo,
        codigo_trabajador,
        token_fcm,
        plataforma,
        modelo_dispositivo,
        version_app,
        version_so,
        activo,
        notif_nuevas,
        notif_aprobadas,
        notif_rechazadas
    FROM ppavac_dispositivo
    WHERE token_fcm = ?;
"""

INSERT_DISPOSITIVO = """
    INSERT INTO ppavac_dispositivo (
        codigo_trabajador, token_fcm, plataforma, modelo_dispositivo,
        version_app, version_so, activo, fecha_registro, fecha_ultimo_acceso,
        notif_nuevas, notif_aprobadas, notif_rechazadas
    )
    OUTPUT INSERTED.id_dispositivo, INSERTED.codigo_trabajador, INSERTED.token_fcm
    VALUES (?, ?, ?, ?, ?, ?, 'S', GETDATE(), GETDATE(), 'S', 'S', 'S');
"""

UPDATE_DISPOSITIVO_TOKEN = """
    UPDATE ppavac_dispositivo
    SET fecha_ultimo_acceso = GETDATE(),
        activo = 'S',
        codigo_trabajador = ?,
        modelo_dispositivo = COALESCE(?, modelo_dispositivo),
        version_app = COALESCE(?, version_app),
        version_so = COALESCE(?, version_so)
    OUTPUT INSERTED.id_dispositivo, INSERTED.codigo_trabajador, INSERTED.token_fcm
    WHERE token_fcm = ?;
"""

SELECT_TOKENS_APROBADORES = """
    SELECT DISTINCT d.token_fcm, d.codigo_trabajador, j.nivel_jerarquico
    FROM ppavac_dispositivo d
    INNER JOIN ppavac_jerarquia j ON d.codigo_trabajador COLLATE DATABASE_DEFAULT = j.codigo_trabajador_aprobador COLLATE DATABASE_DEFAULT
    WHERE j.codigo_area = ?
      AND j.activo = 'S'
      AND (j.fecha_hasta IS NULL OR j.fecha_hasta >= GETDATE())
      AND d.activo = 'S'
      AND (d.notif_nuevas = 'S' OR d.notif_nuevas IS NULL)
      AND d.token_fcm IS NOT NULL
      AND d.token_fcm != ''
    ORDER BY j.nivel_jerarquico ASC;
"""

SELECT_APROBADORES_POR_TRABAJADOR = """
    SELECT DISTINCT 
        j.codigo_trabajador_aprobador,
        j.nivel_jerarquico
    FROM ppavac_jerarquia j
    INNER JOIN dbo.vw_mtraba10 t ON t.careas COLLATE DATABASE_DEFAULT = j.codigo_area COLLATE DATABASE_DEFAULT
    WHERE t.ctraba = ?
      AND j.activo = 'S'
      AND (j.fecha_hasta IS NULL OR j.fecha_hasta >= GETDATE())
      AND (j.codigo_seccion IS NULL OR j.codigo_seccion = t.csecci COLLATE DATABASE_DEFAULT)
      AND (j.codigo_cargo IS NULL OR j.codigo_cargo = t.ccargo COLLATE DATABASE_DEFAULT)
    ORDER BY j.nivel_jerarquico ASC;
"""

SELECT_TOKENS_BY_CODIGOS_TRABAJADORES = """
    SELECT token_fcm, codigo_trabajador
    FROM ppavac_dispositivo
    WHERE codigo_trabajador IN ({})
      AND activo = 'S'
      AND notif_nuevas = 'S';
"""

# Query helper para obtener área del trabajador
SELECT_AREA_TRABAJADOR = """
    SELECT careas AS codigo_area
    FROM dbo.vw_mtraba10
    WHERE ctraba = ?;
"""

# ============================================
# QUERIES PARA AUTENTICACIÓN CONTRA usuarios_web00
# ============================================

# Autenticar usuario contra tabla usuarios_web00 (contraseña en texto plano)
AUTHENTICATE_CLIENTE_USER = """
    SELECT 
        cusuar AS nombre_usuario,
        dclave AS contrasena,
        ctraba AS codigo_trabajador
    FROM usuarios_web00
    WHERE cusuar = ?;
"""

# Obtener datos completos del usuario desde usuarios_web00 y mtraba_web00 para crear registro
SELECT_CLIENTE_USER_DATA = """
    SELECT 
        a.cusuar AS nombre_usuario,
        a.ctptra AS tipo_trabajador,
        a.dusuar AS descripcion_usuario,
        a.ctraba AS codigo_trabajador,
        a.permiso_remoto AS permiso_remoto,
        b.correo AS correo,
        b.area AS area,
        b.cargo AS cargo,
        b.nombres AS nombre,
        b.apellidos AS apellido,
        b.ntftra AS telefono
    FROM usuarios_web00 a
    INNER JOIN mtraba_web00 b ON a.ctraba = b.ctraba
    WHERE a.cusuar = ?;
"""

# Insertar nuevo usuario desde datos de cliente (sin campos adicionales que no están en tabla usuario)
# Nota: Usamos una cadena vacía para contrasena porque usuarios cliente se autentican contra usuarios_web00
INSERT_USUARIO_FROM_CLIENTE = """
    INSERT INTO usuario (
        nombre_usuario, correo, contrasena, nombre, apellido,
        es_activo, correo_confirmado, es_eliminado,
        origen_datos, codigo_trabajador_externo
    )
    OUTPUT
        INSERTED.usuario_id, INSERTED.nombre_usuario, INSERTED.correo,
        INSERTED.nombre, INSERTED.apellido, INSERTED.es_activo,
        INSERTED.correo_confirmado, INSERTED.fecha_creacion,
        INSERTED.origen_datos, INSERTED.codigo_trabajador_externo
    VALUES (?, ?, '', ?, ?, 1, 0, 0, 'cliente', ?);
"""

# Actualizar contraseña en tabla usuarios_web00 (texto plano)
UPDATE_CLIENTE_PASSWORD = """
    UPDATE usuarios_web00
    SET dclave = ?
    WHERE cusuar = ?;
"""

# Obtener nombre_usuario desde usuario_id para usuarios cliente
GET_NOMBRE_USUARIO_BY_ID = """
    SELECT nombre_usuario, origen_datos, codigo_trabajador_externo
    FROM usuario
    WHERE usuario_id = ? AND es_eliminado = 0;
"""

# ============================================
# QUERIES PARA BOLETAS, CERTIFICADOS Y DOCUMENTOS
# ============================================

# Boletas de pago (B)
SELECT_BOLETA_PAGO = """
    SELECT 
        a.ctraba AS codigo_trabajador,
        a.cannos AS anio,
        a.cmeses AS mes,
        a.nseman AS nseman,
        ROW_NUMBER() OVER (PARTITION BY a.cannos, a.cmeses ORDER BY a.nseman) AS semana,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pbolet00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE a.ctraba = ?
      AND a.cannos = ?
      AND a.cmeses = ?
      AND b.ctpdoc = 'B'
    ORDER BY a.nseman;
"""

SELECT_BOLETAS_PAGO_POR_ANIO = """
    SELECT 
        a.ctraba AS codigo_trabajador,
        a.cannos AS anio,
        a.cmeses AS mes,
        a.nseman AS nseman,
        ROW_NUMBER() OVER (PARTITION BY a.cannos, a.cmeses ORDER BY a.nseman) AS semana,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pbolet00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE a.ctraba = ?
      AND a.cannos = ?
      AND b.ctpdoc = 'B'
    ORDER BY a.cmeses, a.nseman;
"""

# Certificados CTS (C)
SELECT_CERTIFICADOS_CTS = """
    SELECT 
        a.ctraba AS codigo_trabajador,
        a.cannos AS anio,
        a.cmeses AS mes,
        a.nseman AS nseman,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pbolet00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE a.ctraba = ?
      AND a.cannos = ?
      AND b.ctpdoc = 'C'
    ORDER BY a.cmeses, a.nseman;
"""

# Documentos de pago (O) - similar a boletas
SELECT_DOCUMENTO_PAGO = """
    SELECT 
        a.ctraba AS codigo_trabajador,
        a.cannos AS anio,
        a.cmeses AS mes,
        a.nseman AS nseman,
        ROW_NUMBER() OVER (PARTITION BY a.cannos, a.cmeses ORDER BY a.nseman) AS semana,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pbolet00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE a.ctraba = ?
      AND a.cannos = ?
      AND a.cmeses = ?
      AND b.ctpdoc IN ('O', 'C') 
    ORDER BY a.nseman;
"""

SELECT_DOCUMENTO_PAGO_POR_ANIO = """
    SELECT 
        a.ctraba AS codigo_trabajador,
        a.cannos AS anio,
        a.cmeses AS mes,
        a.nseman AS nseman,
        ROW_NUMBER() OVER (PARTITION BY a.cannos, a.cmeses ORDER BY a.nseman) AS semana,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pbolet00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE a.ctraba = ?
      AND a.cannos = ?
      AND b.ctpdoc = 'O'
    ORDER BY a.cmeses, a.nseman;
"""

# Documentos de empresa (E) y avisos de empresa (A) desde pdocum00
SELECT_DOCUMENTOS_EMPRESA = """
    SELECT 
        a.ddcocum,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pdocum00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE b.ctpdoc = 'E';
"""

SELECT_AVISOS_EMPRESA = """
    SELECT 
        a.ddcocum,
        a.darchi AS archivo_pdf_hex,
        b.dtpref AS tipo_documento
    FROM pdocum00 a
    INNER JOIN ttpref00 b ON a.ctpref = b.ctpref
    WHERE b.ctpdoc = 'A';
"""

# ============================================
# QUERIES PARA AVISO GENERAL DE ACEPTACIÓN (AP)
# ============================================

# Aviso AP pendiente por trabajador (saprob = 'N')
SELECT_AVISO_AP_PENDIENTE = """
    SELECT TOP 1
        a.ctraba,
        a.darchi AS archivo_pdf_hex,
        a.faprob,
        a.saprob,
        a.fvisual
    FROM pbolet00 a
    WHERE a.ctraba = ?
      AND a.ctpref = 'AP'
      AND a.saprob = 'N'
    ORDER BY a.cannos DESC, a.cmeses DESC, a.nseman DESC;
"""

# Marcar como visualizado (solo si sigue pendiente y aún no tiene fvisual)
UPDATE_AVISO_AP_VISUALIZADO = """
    UPDATE pbolet00
    SET fvisual = GETDATE()
    OUTPUT INSERTED.fvisual
    WHERE ctraba = ?
      AND ctpref = 'AP'
      AND saprob = 'N'
      AND fvisual IS NULL;
"""

# Aceptar aviso (conforme): saprob -> 'S' y faprob -> fecha/hora actual
UPDATE_AVISO_AP_ACEPTADO = """
    UPDATE pbolet00
    SET
        faprob = GETDATE(),
        saprob = 'S'
    OUTPUT INSERTED.faprob, INSERTED.saprob
    WHERE ctraba = ?
      AND ctpref = 'AP'
      AND saprob = 'N';
"""
