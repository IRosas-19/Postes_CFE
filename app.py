"""Interfaz gráfica (Tkinter) para cargar KMZ a la base de postes y consultarla."""
import os
import queue
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import pymysql

import db
import kmz
import mapa_html
from config import DEDUP_TOLERANCIA_M

try:
    import tkintermapview
except ImportError:  # el mapa es opcional; el resto del programa funciona sin él
    tkintermapview = None

CAPAS = {
    "Calles (OpenStreetMap)": ("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
    "Satélite (Esri)": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 19),
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Base de postes de fibra")
        self.geometry("1180x700")
        self.archivos = []
        self.cola = queue.Queue()
        self.mapa_planos = {}
        self.mapa_rutas = {}
        self.ruta_actual = None
        self._conn_ui = None          # conexión reutilizada (no se abre una por clic)
        self._lock = threading.Lock()
        self._token = 0               # identifica la última ruta pedida
        self._pendiente = None

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_imp = ttk.Frame(nb, padding=10)
        self.tab_con = ttk.Frame(nb, padding=10)
        nb.add(self.tab_imp, text="Importar KMZ")
        nb.add(self.tab_con, text="Consultar / Exportar")
        self._armar_importar()
        self._armar_consultar()

        self.after(100, self._procesar_cola)
        self.after(300, self._refrescar_todo)

    # ---------- utilidades ----------
    def _consultar(self, funcion, *args):
        """Ejecuta una consulta con la conexión compartida; si se cayó, reconecta una vez."""
        with self._lock:
            for intento in (1, 2):
                try:
                    if self._conn_ui is None:
                        self._conn_ui = db.conectar(autocommit=True)
                    return funcion(self._conn_ui, *args)
                except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                    try:
                        if self._conn_ui is not None:
                            self._conn_ui.close()
                    except Exception:
                        pass
                    self._conn_ui = None
                    if intento == 2:
                        raise

    def _consultar_ui(self, funcion, *args, silencioso=False):
        """Como _consultar, pero para el hilo de la ventana: muestra el error y devuelve None."""
        try:
            return self._consultar(funcion, *args)
        except Exception as e:
            if not silencioso:
                messagebox.showerror("Base de datos", str(e))
            return None

    def _datos_arbol(self, conn):
        return db.listar(conn), db.listar_rutas(conn), db.estadisticas(conn)

    def _log(self, texto):
        self.log.configure(state="normal")
        self.log.insert("end", texto + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- pestaña Importar ----------
    def _armar_importar(self):
        f = self.tab_imp
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Estado:").grid(row=0, column=0, sticky="w", pady=4)
        self.var_estado = tk.StringVar()
        self.cb_estado = ttk.Combobox(f, textvariable=self.var_estado)
        self.cb_estado.grid(row=0, column=1, sticky="ew", pady=4)
        self.cb_estado.bind("<<ComboboxSelected>>", lambda e: self._actualizar_zonas())
        self.cb_estado.bind("<FocusOut>", lambda e: self._actualizar_zonas())

        ttk.Label(f, text="Zona:").grid(row=1, column=0, sticky="w", pady=4)
        self.var_zona = tk.StringVar()
        self.cb_zona = ttk.Combobox(f, textvariable=self.var_zona)
        self.cb_zona.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(f, text="Archivo(s):").grid(row=2, column=0, sticky="w", pady=4)
        fila = ttk.Frame(f)
        fila.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(fila, text="Elegir KMZ/KML...", command=self._elegir_archivos).pack(side="left")
        self.lbl_archivos = ttk.Label(fila, text="Ninguno seleccionado")
        self.lbl_archivos.pack(side="left", padx=8)

        ttk.Label(f, text="Nombre del plano:").grid(row=3, column=0, sticky="w", pady=4)
        self.var_plano = tk.StringVar()
        self.ent_plano = ttk.Entry(f, textvariable=self.var_plano, state="disabled")
        self.ent_plano.grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(f, text="Tolerancia (m):").grid(row=4, column=0, sticky="w", pady=4)
        self.var_tol = tk.StringVar(value=str(DEDUP_TOLERANCIA_M))
        ttk.Spinbox(f, from_=0, to=10, increment=0.5, width=8, textvariable=self.var_tol).grid(
            row=4, column=1, sticky="w", pady=4)

        self.btn_importar = ttk.Button(f, text="Importar a la base", command=self._importar)
        self.btn_importar.grid(row=5, column=0, columnspan=2, pady=10)

        self.log = ScrolledText(f, height=14, state="disabled")
        self.log.grid(row=6, column=0, columnspan=2, sticky="nsew")
        f.rowconfigure(6, weight=1)

    def _refrescar_estados(self):
        estados = self._consultar_ui(db.listar_estados)
        if estados is not None:
            self.cb_estado["values"] = estados

    def _actualizar_zonas(self):
        estado = self.var_estado.get().strip()
        if not estado:
            return
        zonas = self._consultar_ui(db.listar_zonas, estado, silencioso=True)
        if zonas is not None:
            self.cb_zona["values"] = zonas

    def _elegir_archivos(self):
        rutas = filedialog.askopenfilenames(
            title="Elige uno o varios KMZ/KML",
            filetypes=[("KMZ/KML", "*.kmz *.kml"), ("Todos", "*.*")],
        )
        if not rutas:
            return
        self.archivos = list(rutas)
        if len(self.archivos) == 1:
            nombre = os.path.basename(self.archivos[0])
            self.lbl_archivos.configure(text=nombre)
            self.ent_plano.configure(state="normal")
            self.var_plano.set(os.path.splitext(nombre)[0])
        else:
            self.lbl_archivos.configure(text=f"{len(self.archivos)} archivos (cada uno será un plano)")
            self.var_plano.set("")
            self.ent_plano.configure(state="disabled")

    def _importar(self):
        estado = self.var_estado.get().strip()
        zona = self.var_zona.get().strip()
        if not estado or not zona:
            messagebox.showwarning("Faltan datos", "Escribe o elige el Estado y la Zona.")
            return
        if not self.archivos:
            messagebox.showwarning("Faltan datos", "Elige al menos un archivo KMZ/KML.")
            return
        nombre_unico = None
        if len(self.archivos) == 1:
            nombre_unico = self.var_plano.get().strip()
            if not nombre_unico:
                messagebox.showwarning("Faltan datos", "Escribe el nombre del plano.")
                return
        try:
            tol = float(self.var_tol.get())
        except ValueError:
            messagebox.showwarning("Dato inválido", "La tolerancia debe ser un número (metros).")
            return
        self.btn_importar.configure(state="disabled")
        self._log(f"Importando {len(self.archivos)} archivo(s) en {estado} / {zona}...")
        threading.Thread(
            target=self._trabajo, args=(estado, zona, list(self.archivos), nombre_unico, tol), daemon=True
        ).start()

    def _trabajo(self, estado, zona, archivos, nombre_unico, tol):
        try:
            conn = db.conectar()
        except Exception as e:
            self.cola.put(("log", f"No se pudo conectar a la base: {e}"))
            self.cola.put(("fin", None))
            return
        try:
            for ruta_archivo in archivos:
                base = os.path.basename(ruta_archivo)
                plano = nombre_unico or os.path.splitext(base)[0]
                try:
                    rutas = kmz.parsear(ruta_archivo)
                    if not rutas:
                        self.cola.put(("log", f"{base}: no se encontraron rutas ni postes."))
                        continue
                    r = db.importar_plano(conn, estado, zona, plano, base, rutas, tol)
                    self.cola.put(("log",
                        f"OK  {plano}: {r['rutas']} rutas, {r['referencias']} postes "
                        f"({r['nuevos']} nuevos, {r['reutilizados']} ya existían)"))
                except db.PlanoExistente as e:
                    self.cola.put(("log", str(e)))
                except Exception as e:
                    self.cola.put(("log", f"ERROR en {base}: {e}"))
        finally:
            conn.close()
            self.cola.put(("fin", None))

    def _procesar_cola(self):
        try:
            while True:
                tipo, dato = self.cola.get_nowait()
                if tipo == "log":
                    self._log(dato)
                elif tipo == "fin":
                    self.btn_importar.configure(state="normal")
                    self._log("Listo.")
                    self._refrescar_todo()
                elif tipo == "ruta":
                    token, ruta_id, datos, t_consulta = dato
                    if token == self._token:  # ignora respuestas de rutas que ya no son la elegida
                        self._pintar_ruta(ruta_id, datos, t_consulta)
                elif tipo == "ruta_error":
                    token, mensaje = dato
                    if token == self._token:
                        self.lbl_info.configure(text=f"No se pudo cargar la ruta: {mensaje}")
        except queue.Empty:
            pass
        self.after(100, self._procesar_cola)

    # ---------- pestaña Consultar ----------
    def _armar_consultar(self):
        f = self.tab_con
        barra = ttk.Frame(f)
        barra.pack(fill="x")
        ttk.Button(barra, text="Actualizar", command=self._refrescar_todo).pack(side="left")
        ttk.Button(barra, text="Exportar plano seleccionado a KMZ...", command=self._exportar).pack(side="left", padx=8)
        ttk.Button(barra, text="Ver ruta en el navegador (rápido)", command=self._abrir_en_navegador).pack(side="left")
        self.lbl_stats = ttk.Label(f, text="")
        self.lbl_stats.pack(fill="x", pady=6)

        panel = ttk.PanedWindow(f, orient="horizontal")
        panel.pack(fill="both", expand=True)

        izq = ttk.Frame(panel)
        self.arbol = ttk.Treeview(izq, columns=("rutas", "postes"), show="tree headings")
        self.arbol.heading("#0", text="Estado / Zona / Plano / Ruta")
        self.arbol.heading("rutas", text="Rutas")
        self.arbol.heading("postes", text="Postes")
        self.arbol.column("#0", width=300)
        self.arbol.column("rutas", width=55, anchor="center")
        self.arbol.column("postes", width=55, anchor="center")
        sb = ttk.Scrollbar(izq, orient="vertical", command=self.arbol.yview)
        self.arbol.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.arbol.pack(side="left", fill="both", expand=True)
        self.arbol.bind("<<TreeviewSelect>>", self._al_seleccionar)
        self.arbol.bind("<Double-1>", lambda e: self._abrir_en_navegador(avisar=False))
        panel.add(izq, weight=1)

        der = ttk.Frame(panel)
        panel.add(der, weight=2)
        if tkintermapview is None:
            ttk.Label(
                der, justify="center",
                text="Para ver los postes en el mapa instala el módulo:\n\npip install tkintermapview\n\ny vuelve a abrir el programa.",
            ).pack(expand=True)
            self.mapa = None
            return
        opciones = ttk.Frame(der)
        opciones.pack(fill="x")
        ttk.Label(opciones, text="Mapa:").pack(side="left")
        self.var_capa = tk.StringVar(value="Calles (OpenStreetMap)")
        cb = ttk.Combobox(opciones, textvariable=self.var_capa, values=list(CAPAS), state="readonly", width=26)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._cambiar_capa())
        ttk.Label(opciones, text="●  poste de una sola ruta     ", foreground="#d98200").pack(side="left", padx=(12, 0))
        ttk.Label(opciones, text="●  poste compartido con otra(s) ruta(s)", foreground="#c62828").pack(side="left")
        self.mapa = tkintermapview.TkinterMapView(der, corner_radius=0)
        self.mapa.pack(fill="both", expand=True, pady=4)
        self.mapa.set_position(19.04, -98.20)
        self.mapa.set_zoom(12)
        self.lbl_info = ttk.Label(der, text="Selecciona una ruta del árbol para ver sus postes.", wraplength=620)
        self.lbl_info.pack(fill="x")

    def _abrir_en_navegador(self, avisar=True):
        sel = self.arbol.selection()
        ruta_id = self.mapa_rutas.get(sel[0]) if sel else None
        if ruta_id is None:
            if avisar:
                messagebox.showinfo("Mapa", "Selecciona una ruta del árbol.")
            return
        datos = self._consultar_ui(db.cargar_ruta, ruta_id)
        compartidas = self._consultar_ui(db.rutas_compartidas, ruta_id)
        if datos is None or compartidas is None:
            return
        titulo = f"{self.arbol.item(self.arbol.parent(sel[0]), 'text')} / {datos['nombre']}"
        archivo = Path(tempfile.gettempdir()) / f"ruta_{ruta_id}.html"
        archivo.write_text(mapa_html.generar_html(titulo, datos, compartidas), encoding="utf-8")
        webbrowser.open(archivo.as_uri())

    def _cambiar_capa(self):
        url, max_zoom = CAPAS[self.var_capa.get()]
        self.mapa.set_tile_server(url, max_zoom=max_zoom)

    def _al_seleccionar(self, _evento=None):
        if self.mapa is None:
            return
        # Espera un instante: si el usuario recorre el árbol con las flechas, solo se carga la última.
        if self._pendiente is not None:
            self.after_cancel(self._pendiente)
        self._pendiente = self.after(150, self._cargar_seleccion)

    def _cargar_seleccion(self):
        self._pendiente = None
        sel = self.arbol.selection()
        if not sel:
            return
        ruta_id = self.mapa_rutas.get(sel[0])
        if ruta_id is not None:
            self._dibujar_ruta(ruta_id)

    def _dibujar_ruta(self, ruta_id):
        # La consulta corre en segundo plano para que la ventana no se congele.
        self._token += 1
        self.lbl_info.configure(text="Cargando ruta...")
        threading.Thread(target=self._cargar_ruta_hilo, args=(ruta_id, self._token), daemon=True).start()

    def _cargar_ruta_hilo(self, ruta_id, token):
        t0 = time.perf_counter()
        try:
            datos = self._consultar(db.cargar_ruta, ruta_id)
        except Exception as e:
            self.cola.put(("ruta_error", (token, str(e))))
            return
        self.cola.put(("ruta", (token, ruta_id, datos, time.perf_counter() - t0)))

    def _pintar_ruta(self, ruta_id, datos, t_consulta):
        t0 = time.perf_counter()
        self.ruta_actual = ruta_id
        self.mapa.delete_all_marker()
        self.mapa.delete_all_path()
        t_limpiar = time.perf_counter() - t0
        postes = datos["postes"]
        if not postes:
            self.lbl_info.configure(text=f"{datos['nombre']}: la ruta no tiene postes.")
            return

        # Primero se encuadra el mapa y después se dibuja, para no redibujar todo al hacer zoom.
        lats = [p["lat"] for p in postes]
        lons = [p["lon"] for p in postes]
        if len(postes) == 1:
            self.mapa.set_position(lats[0], lons[0])
            self.mapa.set_zoom(19)
        else:
            self.mapa.fit_bounding_box((max(lats), min(lons)), (min(lats), max(lons)))

        t_encuadre = time.perf_counter() - t0 - t_limpiar

        # Línea de la ruta: su trayectoria si existe; si no, los postes unidos en orden.
        lineas = [[(lat, lon) for lon, lat in t["coords"]] for t in datos["trayectorias"] if len(t["coords"]) > 1]
        if not lineas and len(postes) > 1:
            lineas = [[(p["lat"], p["lon"]) for p in postes]]
        for linea in lineas:
            self.mapa.set_path(linea, color="#1e88e5", width=3)
        t_linea = time.perf_counter() - t0 - t_limpiar - t_encuadre

        satelite = self.var_capa.get().startswith("Sat")
        for p in postes:
            compartido = p["n_rutas"] > 1
            self.mapa.set_marker(
                p["lat"], p["lon"], text=str(p["nombre"]),
                marker_color_circle="#8e1b1b" if compartido else "#a35c00",
                marker_color_outside="#c62828" if compartido else "#d98200",
                text_color="#ffffff" if satelite else "#000000",
                command=lambda marcador, poste=p: self._info_poste(poste),
            )

        compartidos = sum(1 for p in postes if p["n_rutas"] > 1)
        self.lbl_info.configure(
            text=f"{datos['nombre']}: {len(postes)} postes, {compartidos} compartidos con otras rutas. "
                 f"Clic en un poste para ver el detalle.  "
                 f"[consulta {t_consulta:.2f} s · encuadre {t_encuadre:.2f} · línea {t_linea:.2f} · "
                 f"postes {time.perf_counter() - t0 - t_limpiar - t_encuadre - t_linea:.2f}]"
        )

    def _info_poste(self, p):
        texto = f"Poste {p['nombre']} (posición {p['orden']}) · {p['lat']:.7f}, {p['lon']:.7f}"
        if p["n_rutas"] > 1:
            otras = self._consultar_ui(db.rutas_de_poste, p["poste_id"], self.ruta_actual) or []
            nombres = ", ".join(f"{o['plano']} / {o['ruta']}" for o in otras[:8])
            if len(otras) > 8:
                nombres += f" y {len(otras) - 8} más"
            texto += f" · También lo usan: {nombres}"
        else:
            texto += " · Solo lo usa esta ruta."
        self.lbl_info.configure(text=texto)

    def _refrescar_todo(self):
        self._refrescar_estados()
        self._actualizar_zonas()
        self._refrescar_consulta()

    def _refrescar_consulta(self):
        datos = self._consultar_ui(self._datos_arbol)
        if datos is None:
            return
        filas, rutas, est = datos
        self.arbol.delete(*self.arbol.get_children())
        self.mapa_planos.clear()
        self.mapa_rutas.clear()
        estados, zonas, planos = {}, {}, {}
        for f in filas:
            if f["estado"] not in estados:
                estados[f["estado"]] = self.arbol.insert("", "end", text=f["estado"], open=True)
            clave_z = (f["estado"], f["zona"])
            if clave_z not in zonas:
                zonas[clave_z] = self.arbol.insert(estados[f["estado"]], "end", text=f["zona"], open=True)
            iid = self.arbol.insert(zonas[clave_z], "end", text=f["plano"], values=(f["rutas"], f["postes"]))
            planos[(f["estado"], f["zona"], f["plano"])] = iid
            self.mapa_planos[iid] = (f["estado"], f["zona"], f["plano"])
        for r in rutas:
            padre = planos.get((r["estado"], r["zona"], r["plano"]))
            if padre is None:
                continue
            iid = self.arbol.insert(padre, "end", text=r["ruta"], values=("", r["postes"]))
            self.mapa_rutas[iid] = r["ruta_id"]
        self.lbl_stats.configure(
            text=f"Total: {est['postes']} postes únicos · {est['rutas']} rutas · "
                 f"{est['compartidos']} postes compartidos por 2 o más rutas"
        )

    def _exportar(self):
        sel = self.arbol.selection()
        if not sel or sel[0] not in self.mapa_planos:
            messagebox.showinfo("Exportar", "Selecciona un plano (el nivel debajo de la zona).")
            return
        estado, zona, plano = self.mapa_planos[sel[0]]
        destino = filedialog.asksaveasfilename(
            defaultextension=".kmz", initialfile=f"{plano}.kmz", filetypes=[("KMZ", "*.kmz")]
        )
        if not destino:
            return
        rutas = self._consultar_ui(db.cargar_plano, estado, zona, plano)
        if rutas is None:
            return
        kmz.generar_kmz(plano, rutas, destino)
        messagebox.showinfo("Exportar", f"Archivo guardado:\n{destino}")

if __name__ == "__main__":
    App().mainloop()