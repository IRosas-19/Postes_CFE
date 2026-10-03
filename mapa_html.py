"""Genera una página HTML (Leaflet) para ver una ruta en el navegador. Es instantánea
aunque la ruta tenga cientos de postes, porque el navegador dibuja el mapa."""
import html
import json

PLANTILLA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITULO__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<style>
  html, body, #map { height: 100%; margin: 0; }
  .num { background: transparent; border: 0; box-shadow: none; font-weight: bold; color: #000;
         text-shadow: 0 0 3px #fff, 0 0 3px #fff, 0 0 3px #fff; }
  .num:before { display: none; }
  .leyenda { background: rgba(255,255,255,.92); padding: 8px 10px; border-radius: 6px;
             font: 13px/1.5 sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
  .punto { display: inline-block; width: 11px; height: 11px; border-radius: 50%; margin-right: 6px;
           border: 1px solid #fff; vertical-align: middle; }
</style></head>
<body><div id="map"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
const DATOS = __DATOS__;
const calles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 21, maxNativeZoom: 19, attribution: '&copy; OpenStreetMap'});
const satelite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom: 21, maxNativeZoom: 19, attribution: 'Esri World Imagery'});
const map = L.map('map', {layers: [calles]});
L.control.layers({'Calles': calles, 'Satélite': satelite}).addTo(map);
const grupo = L.featureGroup().addTo(map);
DATOS.lineas.forEach(function (l) { L.polyline(l, {color: '#1e88e5', weight: 3}).addTo(grupo); });
DATOS.postes.forEach(function (p) {
  const m = L.circleMarker([p.lat, p.lon], {radius: 7, color: '#fff', weight: 2,
    fillColor: p.compartido ? '#c62828' : '#d98200', fillOpacity: 1}).addTo(grupo);
  m.bindPopup(p.popup);
  m.bindTooltip(p.nombre, {permanent: true, direction: 'top', offset: [0, -6], className: 'num'});
});
if (DATOS.postes.length) { map.fitBounds(grupo.getBounds().pad(0.1), {maxZoom: 19}); }
else { map.setView([19.04, -98.20], 12); }
const leyenda = L.control({position: 'bottomleft'});
leyenda.onAdd = function () {
  const d = L.DomUtil.create('div', 'leyenda');
  d.innerHTML = '<b>' + DATOS.titulo + '</b><br>' + DATOS.postes.length + ' postes, ' + DATOS.compartidos +
    ' compartidos<br><span class="punto" style="background:#d98200"></span>una sola ruta<br>' +
    '<span class="punto" style="background:#c62828"></span>compartido con otras rutas';
  return d;
};
leyenda.addTo(map);
</script></body></html>
"""


def generar_html(titulo, datos, compartidas):
    """
    datos: resultado de db.cargar_ruta; compartidas: resultado de db.rutas_compartidas.
    Devuelve el texto HTML.
    """
    postes = []
    for p in datos["postes"]:
        otras = compartidas.get(p["poste_id"], [])
        popup = (f"<b>Poste {html.escape(str(p['nombre']))}</b> (posición {p['orden']})<br>"
                 f"{p['lat']:.7f}, {p['lon']:.7f}<br>")
        if otras:
            lista = "".join(f"<li>{html.escape(o)}</li>" for o in otras[:15])
            extra = f"<li>y {len(otras) - 15} más</li>" if len(otras) > 15 else ""
            popup += f"También lo usan:<ul style='margin:4px 0 0 16px;padding:0'>{lista}{extra}</ul>"
        else:
            popup += "Solo lo usa esta ruta."
        postes.append({"lat": p["lat"], "lon": p["lon"], "nombre": str(p["nombre"]),
                       "compartido": bool(otras), "popup": popup})
    lineas = [[[lat, lon] for lon, lat in t["coords"]] for t in datos["trayectorias"] if len(t["coords"]) > 1]
    if not lineas and len(postes) > 1:
        lineas = [[[p["lat"], p["lon"]] for p in postes]]
    payload = {"titulo": html.escape(titulo), "postes": postes, "lineas": lineas,
               "compartidos": sum(1 for p in postes if p["compartido"])}
    texto_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return PLANTILLA.replace("__TITULO__", html.escape(titulo)).replace("__DATOS__", texto_json)