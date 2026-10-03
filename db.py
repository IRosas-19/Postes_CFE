"""Capa de base de datos MySQL (pymysql).

Modelo: los postes son únicos (una fila por poste físico) y las rutas los
referencian en orden, así un mismo poste puede pertenecer a varias rutas.
  estados > zonas > planos > rutas > ruta_postes >< postes
"""
import json
import math

import pymysql

from config import DEDUP_TOLERANCIA_M, obtener_config
from kmz import distancia_m


class PlanoExistente(Exception):
    pass


class PlanoNoEncontrado(Exception):
    pass


def conectar(con_bd=True, autocommit=False):
    cfg = obtener_config()
    if not con_bd:
        cfg.pop("database", None)
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


ESQUEMA = [
    """CREATE TABLE IF NOT EXISTS estados (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nombre VARCHAR(100) NOT NULL UNIQUE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS zonas (
        id INT AUTO_INCREMENT PRIMARY KEY,
        estado_id INT NOT NULL,
        nombre VARCHAR(100) NOT NULL,
        UNIQUE KEY uq_zona (estado_id, nombre),
        FOREIGN KEY (estado_id) REFERENCES estados(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS planos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        zona_id INT NOT NULL,
        nombre VARCHAR(150) NOT NULL,
        archivo_origen VARCHAR(255),
        fecha_importacion DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_plano (zona_id, nombre),
        FOREIGN KEY (zona_id) REFERENCES zonas(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rutas (
        id INT AUTO_INCREMENT PRIMARY KEY,
        plano_id INT NOT NULL,
        nombre VARCHAR(150) NOT NULL,
        orden INT NOT NULL,
        UNIQUE KEY uq_ruta (plano_id, nombre),
        FOREIGN KEY (plano_id) REFERENCES planos(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS postes (
        id INT AUTO_INCREMENT PRIMARY KEY,
        lat DECIMAL(10,7) NOT NULL,
        lon DECIMAL(10,7) NOT NULL,
        fecha_alta DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_coord (lat, lon)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS ruta_postes (
        ruta_id INT NOT NULL,
        orden INT NOT NULL,
        poste_id INT NOT NULL,
        nombre VARCHAR(100) NOT NULL,
        PRIMARY KEY (ruta_id, orden),
        INDEX idx_poste (poste_id),
        FOREIGN KEY (ruta_id) REFERENCES rutas(id) ON DELETE CASCADE,
        FOREIGN KEY (poste_id) REFERENCES postes(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS trayectorias (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ruta_id INT NOT NULL,
        nombre VARCHAR(150) NOT NULL,
        coordenadas LONGTEXT NOT NULL,
        FOREIGN KEY (ruta_id) REFERENCES rutas(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def iniciar():
    """Crea la base de datos y las tablas si no existen."""
    nombre_bd = obtener_config()["database"]
    conn = conectar(con_bd=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{nombre_bd}` CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()
    conn = conectar()
    try:
        with conn.cursor() as cur:
            for sentencia in ESQUEMA:
                cur.execute(sentencia)
        conn.commit()
    finally:
        conn.close()


def _id_o_crear(cur, sql_buscar, params_buscar, sql_insertar, params_insertar):
    cur.execute(sql_buscar, params_buscar)
    fila = cur.fetchone()
    if fila:
        return fila["id"]
    cur.execute(sql_insertar, params_insertar)
    return cur.lastrowid


def _poste_cercano(cur, lat, lon, tol_m):
    """Id del poste existente más cercano dentro de tol_m metros, o None."""
    dlat = tol_m / 111320.0
    dlon = tol_m / (111320.0 * max(math.cos(math.radians(lat)), 0.01))
    cur.execute(
        "SELECT id, lat, lon FROM postes WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    )
    mejor = None
    for f in cur.fetchall():
        d = distancia_m(lat, lon, float(f["lat"]), float(f["lon"]))
        if d <= tol_m and (mejor is None or d < mejor[1]):
            mejor = (f["id"], d)
    return mejor[0] if mejor else None


def importar_plano(conn, estado, zona, plano, archivo_origen, rutas, tolerancia_m=DEDUP_TOLERANCIA_M):
    """
    Guarda un plano. Cada poste del archivo se reutiliza si ya hay uno a menos de
    `tolerancia_m` metros; si no, se crea. Todo ocurre en una sola transacción.
    Devuelve {"rutas", "referencias", "nuevos", "reutilizados"}.
    """
    resumen = {"rutas": 0, "referencias": 0, "nuevos": 0, "reutilizados": 0}
    try:
        with conn.cursor() as cur:
            estado_id = _id_o_crear(
                cur, "SELECT id FROM estados WHERE nombre=%s", (estado,),
                "INSERT INTO estados (nombre) VALUES (%s)", (estado,),
            )
            zona_id = _id_o_crear(
                cur, "SELECT id FROM zonas WHERE estado_id=%s AND nombre=%s", (estado_id, zona),
                "INSERT INTO zonas (estado_id, nombre) VALUES (%s, %s)", (estado_id, zona),
            )
            cur.execute("SELECT id FROM planos WHERE zona_id=%s AND nombre=%s", (zona_id, plano))
            if cur.fetchone():
                raise PlanoExistente(f"El plano '{plano}' ya existe en {estado} / {zona}; se omitió.")
            cur.execute(
                "INSERT INTO planos (zona_id, nombre, archivo_origen) VALUES (%s, %s, %s)",
                (zona_id, plano, archivo_origen),
            )
            plano_id = cur.lastrowid
            for orden_ruta, (nombre_ruta, datos) in enumerate(rutas.items(), start=1):
                cur.execute(
                    "INSERT INTO rutas (plano_id, nombre, orden) VALUES (%s, %s, %s)",
                    (plano_id, nombre_ruta, orden_ruta),
                )
                ruta_id = cur.lastrowid
                resumen["rutas"] += 1
                for orden, p in enumerate(datos["postes"], start=1):
                    poste_id = _poste_cercano(cur, p["lat"], p["lon"], tolerancia_m)
                    if poste_id is None:
                        cur.execute("INSERT INTO postes (lat, lon) VALUES (%s, %s)", (p["lat"], p["lon"]))
                        poste_id = cur.lastrowid
                        resumen["nuevos"] += 1
                    else:
                        resumen["reutilizados"] += 1
                    cur.execute(
                        "INSERT INTO ruta_postes (ruta_id, orden, poste_id, nombre) VALUES (%s, %s, %s, %s)",
                        (ruta_id, orden, poste_id, p["nombre"]),
                    )
                    resumen["referencias"] += 1
                tray = [(ruta_id, t["nombre"], json.dumps(t["coords"])) for t in datos["trayectorias"]]
                if tray:
                    cur.executemany(
                        "INSERT INTO trayectorias (ruta_id, nombre, coordenadas) VALUES (%s, %s, %s)", tray
                    )
        conn.commit()
        return resumen
    except Exception:
        conn.rollback()
        raise


def listar_estados(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT nombre FROM estados ORDER BY nombre")
        return [f["nombre"] for f in cur.fetchall()]


def listar_zonas(conn, estado):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT z.nombre FROM zonas z JOIN estados e ON e.id = z.estado_id "
            "WHERE e.nombre=%s ORDER BY z.nombre",
            (estado,),
        )
        return [f["nombre"] for f in cur.fetchall()]


def listar(conn):
    """Un renglón por plano: estado, zona, plano, rutas y postes distintos."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.nombre AS estado, z.nombre AS zona, p.nombre AS plano,
                      COUNT(DISTINCT r.id) AS rutas, COUNT(DISTINCT rp.poste_id) AS postes
               FROM planos p
               JOIN zonas z ON z.id = p.zona_id
               JOIN estados e ON e.id = z.estado_id
               LEFT JOIN rutas r ON r.plano_id = p.id
               LEFT JOIN ruta_postes rp ON rp.ruta_id = r.id
               GROUP BY e.nombre, z.nombre, p.id, p.nombre
               ORDER BY e.nombre, z.nombre, p.nombre"""
        )
        return cur.fetchall()


def estadisticas(conn):
    """Totales de la base: postes únicos, rutas y postes compartidos por 2+ rutas."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM postes")
        postes = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM rutas")
        rutas = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM (SELECT poste_id FROM ruta_postes "
            "GROUP BY poste_id HAVING COUNT(DISTINCT ruta_id) > 1) t"
        )
        compartidos = cur.fetchone()["n"]
    return {"postes": postes, "rutas": rutas, "compartidos": compartidos}


def cargar_plano(conn, estado, zona, plano):
    """Devuelve las rutas del plano con sus postes (en orden) y trayectorias."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.id FROM planos p
               JOIN zonas z ON z.id = p.zona_id
               JOIN estados e ON e.id = z.estado_id
               WHERE e.nombre=%s AND z.nombre=%s AND p.nombre=%s""",
            (estado, zona, plano),
        )
        fila = cur.fetchone()
        if not fila:
            raise PlanoNoEncontrado(f"No existe el plano '{plano}' en {estado} / {zona}.")
        cur.execute("SELECT id, nombre FROM rutas WHERE plano_id=%s ORDER BY orden", (fila["id"],))
        rutas = []
        for r in cur.fetchall():
            cur.execute(
                "SELECT rp.nombre AS nombre, po.lat AS lat, po.lon AS lon FROM ruta_postes rp "
                "JOIN postes po ON po.id = rp.poste_id WHERE rp.ruta_id=%s ORDER BY rp.orden",
                (r["id"],),
            )
            postes = [{"nombre": p["nombre"], "lat": float(p["lat"]), "lon": float(p["lon"])} for p in cur.fetchall()]
            cur.execute("SELECT nombre, coordenadas FROM trayectorias WHERE ruta_id=%s", (r["id"],))
            tray = [{"nombre": t["nombre"], "coords": json.loads(t["coordenadas"])} for t in cur.fetchall()]
            rutas.append({"nombre": r["nombre"], "postes": postes, "trayectorias": tray})
        return rutas


def listar_rutas(conn):
    """Un renglón por ruta (con su estado/zona/plano) y cuántos postes tiene."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.nombre AS estado, z.nombre AS zona, p.nombre AS plano,
                      r.id AS ruta_id, r.nombre AS ruta, COUNT(rp.poste_id) AS postes
               FROM rutas r
               JOIN planos p ON p.id = r.plano_id
               JOIN zonas z ON z.id = p.zona_id
               JOIN estados e ON e.id = z.estado_id
               LEFT JOIN ruta_postes rp ON rp.ruta_id = r.id
               GROUP BY e.nombre, z.nombre, p.id, p.nombre, r.id, r.nombre, r.orden
               ORDER BY e.nombre, z.nombre, p.nombre, r.orden"""
        )
        return cur.fetchall()


def cargar_ruta(conn, ruta_id):
    """
    Datos de una ruta para dibujarla: sus postes en orden (con cuántas rutas
    usan cada uno) y sus trayectorias [[lon, lat], ...].
    """
    with conn.cursor() as cur:
        cur.execute("SELECT nombre FROM rutas WHERE id=%s", (ruta_id,))
        fila = cur.fetchone()
        if not fila:
            raise PlanoNoEncontrado("La ruta ya no existe en la base.")
        cur.execute(
            """SELECT rp.orden AS orden, rp.nombre AS nombre, po.id AS poste_id,
                      po.lat AS lat, po.lon AS lon,
                      (SELECT COUNT(DISTINCT x.ruta_id) FROM ruta_postes x
                       WHERE x.poste_id = po.id) AS n_rutas
               FROM ruta_postes rp JOIN postes po ON po.id = rp.poste_id
               WHERE rp.ruta_id=%s ORDER BY rp.orden""",
            (ruta_id,),
        )
        postes = [
            {"orden": p["orden"], "nombre": p["nombre"], "poste_id": p["poste_id"],
             "lat": float(p["lat"]), "lon": float(p["lon"]), "n_rutas": int(p["n_rutas"])}
            for p in cur.fetchall()
        ]
        cur.execute("SELECT nombre, coordenadas FROM trayectorias WHERE ruta_id=%s", (ruta_id,))
        tray = [{"nombre": t["nombre"], "coords": json.loads(t["coordenadas"])} for t in cur.fetchall()]
    return {"nombre": fila["nombre"], "postes": postes, "trayectorias": tray}


def rutas_de_poste(conn, poste_id, excluir_ruta_id=None):
    """Otras rutas (plano / ruta) que usan el mismo poste."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT p.nombre AS plano, r.nombre AS ruta, r.id AS ruta_id
               FROM ruta_postes x
               JOIN rutas r ON r.id = x.ruta_id
               JOIN planos p ON p.id = r.plano_id
               WHERE x.poste_id=%s ORDER BY p.nombre, r.nombre""",
            (poste_id,),
        )
        return [f for f in cur.fetchall() if f["ruta_id"] != excluir_ruta_id]


def rutas_compartidas(conn, ruta_id):
    """
    Para cada poste de la ruta que también usan otras rutas, la lista de esas rutas.
    Devuelve {poste_id: ["Plano / Ruta", ...]} en una sola consulta.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT x.poste_id AS poste_id, p.nombre AS plano, r.nombre AS ruta
               FROM ruta_postes x
               JOIN rutas r ON r.id = x.ruta_id
               JOIN planos p ON p.id = r.plano_id
               WHERE x.ruta_id <> %s
                 AND x.poste_id IN (SELECT poste_id FROM ruta_postes WHERE ruta_id = %s)
               ORDER BY p.nombre, r.nombre""",
            (ruta_id, ruta_id),
        )
        resultado = {}
        for f in cur.fetchall():
            resultado.setdefault(f["poste_id"], []).append(f"{f['plano']} / {f['ruta']}")
        return resultado