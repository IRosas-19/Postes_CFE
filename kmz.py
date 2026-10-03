"""Lectura y escritura de KMZ/KML (sin dependencias externas)."""
import math
import os
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


def distancia_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos coordenadas (haversine)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _leer_texto_kml(ruta):
    if ruta.lower().endswith(".kmz"):
        with zipfile.ZipFile(ruta) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("El KMZ no contiene ningún archivo .kml")
            nombre = "doc.kml" if "doc.kml" in kmls else kmls[0]
            return z.read(nombre)
    with open(ruta, "rb") as f:
        return f.read()


def _sin_namespaces(root):
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = el.tag.split("}", 1)[-1]


def _nombre(el):
    n = el.findtext("name")
    return n.strip() if n else ""


def _parsear_coords(texto):
    """'lon,lat,alt lon,lat,alt ...' -> [(lon, lat), ...]"""
    puntos = []
    for trozo in (texto or "").split():
        partes = trozo.split(",")
        if len(partes) >= 2:
            puntos.append((float(partes[0]), float(partes[1])))
    return puntos


def parsear(ruta_archivo):
    """
    Lee un KMZ/KML y devuelve un dict ordenado:
      {ruta: {"postes": [{nombre, lat, lon}], "trayectorias": [{nombre, coords}]}}
    La ruta es la carpeta que contiene a "Postes" / "Trayectoria"
    (o la carpeta directa del Placemark si no usa esa estructura).
    """
    root = ET.fromstring(_leer_texto_kml(ruta_archivo))
    _sin_namespaces(root)
    rutas = {}

    def asegurar(nombre_ruta):
        if nombre_ruta not in rutas:
            rutas[nombre_ruta] = {"postes": [], "trayectorias": []}
        return rutas[nombre_ruta]

    def visitar(elem, carpetas):
        for hijo in elem:
            if hijo.tag == "Folder":
                visitar(hijo, carpetas + [_nombre(hijo)])
            elif hijo.tag == "Document":
                visitar(hijo, carpetas)
            elif hijo.tag == "Placemark":
                if carpetas and carpetas[-1].lower() in ("postes", "trayectoria", "trayectorias") and len(carpetas) >= 2:
                    ruta = carpetas[-2]
                else:
                    ruta = carpetas[-1] if carpetas else "Sin ruta"
                nombre = _nombre(hijo)
                punto = hijo.find(".//Point/coordinates")
                linea = hijo.find(".//LineString/coordinates")
                if punto is not None:
                    c = _parsear_coords(punto.text)
                    if c:
                        asegurar(ruta)["postes"].append({"nombre": nombre, "lat": c[0][1], "lon": c[0][0]})
                elif linea is not None:
                    c = _parsear_coords(linea.text)
                    if c:
                        asegurar(ruta)["trayectorias"].append({"nombre": nombre, "coords": [list(p) for p in c]})

    visitar(root, [])
    return rutas


def generar_kmz(nombre_plano, rutas, destino):
    """rutas: lista de {"nombre", "postes": [{nombre, lat, lon}], "trayectorias": [{nombre, coords}]}"""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        f"<name>{escape(nombre_plano)}</name>",
        '<Style id="poste"><IconStyle><Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon></IconStyle></Style>',
        '<Style id="linea"><LineStyle><color>ff06ff21</color><width>4</width></LineStyle></Style>',
        f"<Folder><name>{escape(nombre_plano)}</name>",
    ]
    for r in rutas:
        out.append(f"<Folder><name>{escape(r['nombre'])}</name>")
        out.append("<Folder><name>Postes</name>")
        for p in r["postes"]:
            out.append(
                f"<Placemark><name>{escape(str(p['nombre']))}</name><styleUrl>#poste</styleUrl>"
                f"<Point><coordinates>{p['lon']:.8f},{p['lat']:.8f},0</coordinates></Point></Placemark>"
            )
        out.append("</Folder>")
        out.append("<Folder><name>Trayectoria</name>")
        for t in r["trayectorias"]:
            coords = " ".join(f"{lon:.8f},{lat:.8f},0" for lon, lat in t["coords"])
            out.append(
                f"<Placemark><name>{escape(t['nombre'])}</name><styleUrl>#linea</styleUrl>"
                f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>"
            )
        out.append("</Folder></Folder>")
    out.append("</Folder></Document></kml>")

    carpeta = os.path.dirname(os.path.abspath(destino))
    os.makedirs(carpeta, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", "\n".join(out))
