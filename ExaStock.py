import os
import sys
import json
import unicodedata
import textwrap
import datetime as dt
import threading
import queue
import logging
import logging.handlers
import re
import urllib.request
import zipfile

from mobile_scanner import iniciar_servidor, codigo_queue, conexion_queue, estado_queue, mostrar_ventana_qr

import pandas as pd
import customtkinter as ctk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Sonidos de retroalimentación (solo Windows; en otros sistemas no suena,
# pero la app sigue funcionando igual sin dar error).
# ---------------------------------------------------------------------------
try:
    import winsound

    def _beep(frecuencia, duracion_ms):
        try:
            winsound.Beep(frecuencia, duracion_ms)
        except Exception:
            pass
except ImportError:  # no estamos en Windows
    def _beep(frecuencia, duracion_ms):
        pass


def sonido_ok():
    """Escaneo correcto: artículo contado en la ubicación correcta, o ubicación activada."""
    _beep(1500, 90)


def sonido_alerta():
    """Escaneo válido pero con advertencia: artículo mal ubicado."""
    _beep(900, 130)
    _beep(700, 130)


def sonido_error():
    """Escaneo inválido: código no encontrado, ubicación inválida, o sin ubicación activa."""
    _beep(400, 200)

# ---------------------------------------------------------------------------
# Configuración general
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Configuración ExaStock
# ---------------------------------------------------------------------------
VERSION = "2.2.1"
APP_NAME = "ExaStock"
APP_TITLE = f"ExaStock v{VERSION}"

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

def ruta_recurso(nombre):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre)

BASE_DIR = os.path.join(os.path.expanduser("~"), "ExaStock")
os.makedirs(BASE_DIR, exist_ok=True)

SESSION_FILE = os.path.join(BASE_DIR, "sesion_conteo.json")
SESSIONS_DIR = os.path.join(BASE_DIR, "conteos_guardados")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# Paleta ExaStock – estilo industrial moderno
COLOR_BRAND_PRIMARY = "#1A3A5C"
COLOR_BRAND_ACCENT = "#C9A84C"
COLOR_BRAND_LIGHT = "#F5F0E8"
COLOR_BRAND_DARK = "#0D1B2A"

COLOR_OK = "#C8E6C9"
COLOR_FALTA = "#FFCDD2"
COLOR_SOBRA = "#FFE0B2"
COLOR_TABLA_FONDO = "#FAFAF8"
COLOR_MAL_UBICADO = "#E8D5F5"
COLOR_NOENC = "#E0E0E0"
COLOR_PENDIENTE = COLOR_TABLA_FONDO

ctk.ThemeManager.theme["CTkLabel"]["text_color"] = [COLOR_BRAND_PRIMARY, COLOR_BRAND_PRIMARY]
ctk.ThemeManager.theme["CTkFrame"]["fg_color"] = [COLOR_BRAND_LIGHT, COLOR_BRAND_LIGHT]

REQUIRED_COLS = ["almacen", "ubicacion", "articulo", "descripcion", "existencia"]
SEP = "||"  # separador para armar claves compuestas (ubicacion+articulo) al guardar en JSON

TOLERANCIA = 0.001
PREFIJO_UBICACION = "UBI-"

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _configurar_logger():
    """Configura el logger de actividad: un archivo .log por día."""
    nombre_log = dt.date.today().strftime("conteo_%Y-%m-%d") + ".log"
    ruta_log = os.path.join(LOG_DIR, nombre_log)
    logger = logging.getLogger("conteo_inventario")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            ruta_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


_log = _configurar_logger()


def _es_primera_vez():
    """Devuelve True si es la primera vez que se abre la app."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return not cfg.get("tutorial_visto", False)
        return True
    except Exception:
        return True


def _marcar_tutorial_visto():
    """Marca el tutorial como visto para no volver a mostrarlo."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"tutorial_visto": True}, f)
    except Exception:
        pass


_CACHE_NORMALIZAR = {}


# ── Tooltip helper ────────────────────────────────────────────────────
def crear_tooltip(widget, texto, delay_ms=500):
    """Muestra una pista flotante al pasar el mouse sobre *widget*."""
    _state = {"after_id": None, "ventana": None}

    def _programar(event=None):
        _cancelar()
        _state["after_id"] = widget.after(delay_ms, _mostrar)

    def _mostrar():
        x = widget.winfo_rootx() + 10
        y = widget.winfo_rooty() + widget.winfo_height() + 5
        win = ctk.CTkToplevel(widget)
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        ctk.CTkLabel(
            win, text=texto, fg_color=("#333", "#555"),
            text_color="white", corner_radius=6,
            font=ctk.CTkFont(size=11),
        ).pack(padx=8, pady=4)
        win.geometry(f"+{x}+{y}")
        _state["ventana"] = win

    def _cancelar(event=None):
        if _state["after_id"] is not None:
            widget.after_cancel(_state["after_id"])
            _state["after_id"] = None
        if _state["ventana"] is not None:
            _state["ventana"].destroy()
            _state["ventana"] = None

    widget.bind("<Enter>", _programar, add="+")
    widget.bind("<Leave>", _cancelar, add="+")
    widget.bind("<ButtonPress>", _cancelar, add="+")


def normalizar(texto: str) -> str:
    """Quita acentos, espacios y pasa a minúsculas para comparar códigos/columnas.
    Usa caché para evitar recálculos en operaciones repetitivas."""
    texto_str = str(texto)
    if texto_str in _CACHE_NORMALIZAR:
        return _CACHE_NORMALIZAR[texto_str]
    limpio = texto_str.strip().lower()
    limpio = "".join(
        c for c in unicodedata.normalize("NFD", limpio) if unicodedata.category(c) != "Mn"
    )
    if len(_CACHE_NORMALIZAR) < 50000:
        _CACHE_NORMALIZAR[texto_str] = limpio
    return limpio


def normalizar_columna(texto: str) -> str:
    """Como normalizar(), pero además quita espacios, puntos y guiones (para nombres de columna)."""
    base = normalizar(texto)
    return "".join(ch for ch in base if ch.isalnum())


def fmt_num(x):
    """Formatea un número: entero si es exacto, o con hasta 2 decimales (para kg/lts)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    if x == int(x):
        return int(x)
    return round(x, 2)


def clave(ubicacion, articulo):
    return f"{ubicacion}{SEP}{articulo}"


def parsear_existencia(valor):
    """Convierte el valor de existencia del Excel a número, tal cual viene."""
    if valor is None:
        return 0.0, True
    if isinstance(valor, (int, float)):
        if isinstance(valor, float) and pd.isna(valor):
            return 0.0, True
        return float(valor), True

    texto = str(valor).strip()
    if texto == "" or texto.lower() == "nan":
        return 0.0, True

    texto = texto.replace(" ", "").replace("$", "")

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        partes = texto.split(",")
        if len(partes) == 2 and len(partes[1]) in (1, 2):
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    try:
        return float(texto), True
    except ValueError:
        return 0.0, False


def declave(k):
    return k.split(SEP, 1)


# ---------------------------------------------------------------------------
# Aplicación principal
# ---------------------------------------------------------------------------

class InventarioApp(ctk.CTk):
    """ExaStock – Conteo de inventario preciso y veloz."""

    GITHUB_REPO = "mercedesalmacenm-lang/ExaStock"
    GITHUB_API = f"https://api.github.com/repos/mercedesalmacenm-lang/ExaStock/releases/latest"

    @staticmethod
    def _comprobar_actualizacion():
        """Revisa si hay una versión más reciente en GitHub Releases."""
        try:
            req = urllib.request.Request(
                InventarioApp.GITHUB_API,
                headers={"User-Agent": "ExaStock/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
            latest = data.get("tag_name", "").lstrip("v")
            if not latest:
                return None, None, None
            url_descarga = None
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".exe") or name.endswith(".zip"):
                    url_descarga = asset.get("browser_download_url")
                    break
            return latest, url_descarga, data.get("body", "")
        except Exception:
            return None, None, None

    @staticmethod
    def _es_version_mayor(v_local, v_remota):
        try:
            partes_local = [int(x) for x in re.split(r"[.\-]", v_local) if x.isdigit()]
            partes_remota = [int(x) for x in re.split(r"[.\-]", v_remota) if x.isdigit()]
            for i in range(max(len(partes_local), len(partes_remota))):
                a = partes_local[i] if i < len(partes_local) else 0
                b = partes_remota[i] if i < len(partes_remota) else 0
                if b > a:
                    return True
                if a > b:
                    return False
            return False
        except Exception:
            return False

    @staticmethod
    def _limpiar_instaladores_viejos():
        """Elimina .exe viejos del escritorio, excepto el que está corriendo."""
        import glob as _glob
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        exe_actual = None
        if getattr(sys, 'frozen', False):
            exe_actual = os.path.abspath(sys.executable)
        for f in _glob.glob(os.path.join(desktop, "ExaStock_v*.exe")):
            try:
                if exe_actual and os.path.abspath(f) == exe_actual:
                    continue
                os.remove(f)
            except OSError:
                pass

    def _revisar_actualizacion(self):
        """Se ejecuta al inicio. Si hay actualización, lo muestra."""
        threading.Thread(target=self._revisar_actualizacion_hilo, daemon=True).start()

    def _revisar_actualizacion_hilo(self):
        try:
            version_remota, url_descarga, notas = self._comprobar_actualizacion()
            if not version_remota or not self._es_version_mayor(VERSION, version_remota):
                self.after(0, lambda: self._actualizar_label_update(""))
                return
            self._ultima_version = version_remota
            self._url_descarga = url_descarga
            self._notas_version = notas[:300] if notas else ""
            self.after(0, lambda: self._preguntar_actualizacion())
        except Exception:
            pass

    def _preguntar_actualizacion(self):
        self._actualizar_label_update(f"Nueva versión {self._ultima_version} disponible")
        respuesta = messagebox.askyesno(
            f"Actualización disponible – ExaStock {self._ultima_version}",
            f"Hay una nueva versión de ExaStock.\n\n"
            f"Actual: v{VERSION}\nNueva: v{self._ultima_version}\n\n"
            + (f"Novedades:\n{self._notas_version}\n\n" if self._notas_version else "")
            + "¿Quieres descargar e instalar la actualización?"
        )
        if respuesta and self._url_descarga:
            self._descargar_actualizacion()

    def _actualizar_label_update(self, texto):
        if texto:
            self.lbl_update_status.configure(text=texto, text_color="#C9A84C")
        else:
            self.lbl_update_status.configure(text="")

    def _descargar_actualizacion(self):
        import glob as _glob
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        exe_actual = os.path.abspath(sys.executable) if getattr(sys, 'frozen', False) else None
        for f in _glob.glob(os.path.join(desktop, "ExaStock_v*.exe")):
            try:
                if exe_actual and os.path.abspath(f) == exe_actual:
                    continue
                os.remove(f)
            except OSError:
                pass

        self.set_status("Descargando actualización...")
        hilo = threading.Thread(target=self._descargar_hilo, daemon=True)
        hilo.start()
        self.after(100, lambda: self._mostrar_progreso_descarga(hilo))

    def _mostrar_progreso_descarga(self, hilo):
        ANCHO, ALTO = 400, 150
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Descargando actualización...")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        self._update_win = win
        self._update_lbl = ctk.CTkLabel(
            win, text="Descargando ExaStock v" + self._ultima_version + "...",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self._update_lbl.pack(pady=(25, 5))

        self._update_pct = ctk.CTkLabel(
            win, text="0%", font=ctk.CTkFont(size=12), text_color="#666666"
        )
        self._update_pct.pack(pady=(0, 5))

        self._update_bar = ctk.CTkProgressBar(win, width=320)
        self._update_bar.pack(pady=(0, 15))
        self._update_bar.set(0)

        win.grab_set()
        self._pollar_descarga(hilo)

    def _pollar_descarga(self, hilo):
        if hilo.is_alive():
            if hasattr(self, "_descarga_progreso"):
                pct = self._descarga_progreso
                self._update_bar.set(pct / 100)
                self._update_pct.configure(text=f"{pct:.0f}%")
            self.after(150, lambda: self._pollar_descarga(hilo))
            return
        self._update_bar.set(1.0)
        self._update_pct.configure(text="100%")
        self.after(300, lambda: self._cerrar_progreso_descarga())

    def _cerrar_progreso_descarga(self):
        if hasattr(self, "_update_win") and self._update_win.winfo_exists():
            self._update_win.grab_release()
            self._update_win.destroy()
        self.set_status("Listo")
        if getattr(self, "_descarga_ok", False):
            nuevo = os.path.join(os.path.expanduser("~"), "Desktop", f"ExaStock_v{self._ultima_version}.exe")
            respuesta = messagebox.askyesno(
                "Descarga completada",
                f"ExaStock v{self._ultima_version} se ha descargado.\n\n"
                f"Se abrirá automáticamente.\n"
                "¿Cerrar esta versión antigua ahora?"
            )
            if os.path.exists(nuevo):
                os.startfile(nuevo)
            if respuesta:
                self.after(500, self.destroy)
        else:
            messagebox.showerror(
                "Error de descarga",
                "No se pudo descargar la actualización.\n"
                "Verifica tu conexión a internet."
            )

    def _descargar_hilo(self):
        try:
            self._descarga_progreso = 0
            destino = os.path.join(os.path.expanduser("~"), "Desktop", f"ExaStock_v{self._ultima_version}.exe")
            req = urllib.request.Request(self._url_descarga, headers={"User-Agent": "ExaStock/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                total = int(r.headers.get("Content-Length", 0))
                descargado = 0
                with open(destino, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        descargado += len(chunk)
                        if total > 0:
                            self._descarga_progreso = min(99, (descargado * 100) // total)
            self._descarga_progreso = 100
            self._descarga_ok = True
        except Exception:
            self._descarga_ok = False

    def __init__(self):
        super().__init__()
        self._limpiar_instaladores_viejos()

        self.title(APP_TITLE)
        self.geometry("1200x700")
        self.minsize(1000, 620)
        self._centrar_ventana(1200, 700)

        try:
            self.iconbitmap(ruta_recurso("ExacStock.ico"))
        except Exception:
            pass

        self.configure(fg_color=COLOR_BRAND_LIGHT)

        self.df = None
        self.excel_path = None
        self.ubicacion_activa = None
        self.archivo_json_activo = None

        self.counts = {}
        self.mismatches = {}
        self.no_encontrados = {}

        self.ubicaciones_set = set()
        self.ubicaciones_norm_map = {}
        self.articulos_all_norm = set()
        self.articulos_norm_map = {}
        self.articulos_por_ubicacion = {}
        self._tiene_unidad = False
        self.unidad_actual = "Sin Unidad"
        self.lbl_titulo = None
        self._autoguardado_timer = None
        self._ultimo_ping_escaner = None
        self._ultima_comprobacion = None

        self.filtro_categoria = "Todos"
        self._filtro_timer = None
        self._pagina_actual = 0
        self._filas_por_pagina = 500
        self._total_filas_filtradas = 0
        self.stat_boxes = {}
        self._categoria_por_stat = {
            "total": "Todos",
            "escaneados": "Escaneados",
            "coinciden": "Coinciden",
            "diferencias": "Con diferencia",
            "pendientes": "Pendientes",
            "mal_ubicados": "Mal ubicados",
            "no_encontrados": "No encontrados",
        }

        self._build_ui()
        self._resaltar_stat_activo()
        self._revisar_sesion_previa()
        self.protocol("WM_DELETE_WINDOW", self.cerrar_aplicacion)

        # --- Tutorial primera vez ---
        self.after(500, self._mostrar_tutorial_si_primera_vez)

        # --- Escáner desde celular ---
        self.ip_escaner, self.puerto_escaner = iniciar_servidor()
        self.after(200, self.revisar_cola_escaner)
        self.after(500, self.revisar_estado_escaner)
        _log.info("APP INICIADA | IP=%s:%s", self.ip_escaner, self.puerto_escaner)

        # --- Actualizador automático ---
        self.after(2000, self._revisar_actualizacion)

    def _mostrar_tutorial_si_primera_vez(self):
        if not _es_primera_vez():
            return
        self._abrir_tutorial()

    def _abrir_tutorial(self):
        pasos = [
            {
                "titulo": "Bienvenido a ExaStock",
                "descripcion": (
                    "Este asistente te guiará por los pasos básicos\n"
                    "para usar la aplicación.\n\n"
                    "Haz clic en \"Siguiente\" para continuar."
                ),
            },
            {
                "titulo": "1. Cargar Excel",
                "descripcion": (
                    "Haz clic en \"Cargar Excel\" para seleccionar\n"
                    "el archivo .xlsx del inventario actual.\n\n"
                    "El Excel debe tener las columnas:\n"
                    "almacen, ubicacion, articulo, descripcion, existencia"
                ),
            },
            {
                "titulo": "2. Escanear ubicación",
                "descripcion": (
                    "Siempre escanea PRIMERO la ubicación\n"
                    "con o sin el prefijo UBI-.\n\n"
                    "El banner de arriba cambiará a azul\n"
                    "y mostrará cuántos artículos tiene esa ubicación."
                ),
            },
            {
                "titulo": "3. Escanear artículos",
                "descripcion": (
                    "Después escanea los códigos de barras\n"
                    "de los artículos en esa ubicación.\n\n"
                    "✔ Verde = artículo correcto\n"
                    "✘ Morado = mal ubicado\n"
                    "✘ Rojo = no existe en el reporte"
                ),
            },
            {
                "titulo": "4. Escanear con celular",
                "descripcion": (
                    "Si no tienes lector, usa \"📷 Escanear con celular\".\n"
                    "Escanea el QR con la cámara de tu celular\n"
                    "y apunta a los códigos de barras.\n\n"
                    "Tu celular y PC deben estar en el mismo WiFi."
                ),
            },
            {
                "titulo": "5. Editar y exportar",
                "descripcion": (
                    "Doble clic en cualquier fila para\n"
                    "corregir la cantidad contada.\n\n"
                    "Usa \"Exportar resultados\" para generar\n"
                    "un Excel con el resumen del conteo.\n\n"
                    "El progreso se guarda automáticamente."
                ),
            },
            {
                "titulo": "¡Listo!",
                "descripcion": (
                    "Ya puedes empezar a contar inventario.\n\n"
                    "Si necesitas ayuda, usa el botón\n"
                    "\"❓ Ayuda\" en la esquina superior derecha."
                ),
            },
        ]
        self._tutorial_paso = 0
        self._tutorial_pasos = pasos
        self._tutorial_ventana = None
        self._mostrar_paso_tutorial()

    def _mostrar_paso_tutorial(self):
        paso = self._tutorial_paso
        pasos = self._tutorial_pasos
        if paso >= len(pasos):
            self._cerrar_tutorial()
            return

        if self._tutorial_ventana is not None and self._tutorial_ventana.winfo_exists():
            pass
        else:
            self._crear_ventana_tutorial()

        data = pasos[paso]
        self._tutorial_label_titulo.configure(text=data["titulo"])
        self._tutorial_label_desc.configure(text=data["descripcion"])
        self._tutorial_label_paso.configure(text=f"Paso {paso + 1} de {len(pasos)}")
        self._tutorial_btn_siguiente.configure(
            text="Comenzar" if paso == 0 else ("Finalizar" if paso == len(pasos) - 1 else "Siguiente →")
        )

    def _crear_ventana_tutorial(self):
        ANCHO, ALTO = 480, 420
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Tutorial - ExaStock")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)

        ctk.CTkLabel(
            win, text="🎓 Tutorial interactivo",
            font=ctk.CTkFont(size=18, weight="bold")
        ).pack(pady=(25, 10))

        separador = ctk.CTkFrame(win, height=2, fg_color="#E3DCC8")
        separador.pack(fill="x", padx=30, pady=(0, 15))

        self._tutorial_label_titulo = ctk.CTkLabel(
            win, text="", font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=440
        )
        self._tutorial_label_titulo.pack(pady=(0, 10), padx=30)

        self._tutorial_label_desc = ctk.CTkLabel(
            win, text="", font=ctk.CTkFont(size=13),
            wraplength=440, justify="left"
        )
        self._tutorial_label_desc.pack(pady=(0, 15), padx=30, fill="both", expand=True)

        self._tutorial_label_paso = ctk.CTkLabel(
            win, text="", font=ctk.CTkFont(size=11),
            text_color="#888888"
        )
        self._tutorial_label_paso.pack(pady=(0, 8))

        btns_frame = ctk.CTkFrame(win, fg_color="transparent")
        btns_frame.pack(pady=(5, 20))

        ctk.CTkButton(
            btns_frame, text="Saltar tutorial", width=100,
            fg_color="#A6ACAF", hover_color="#7F8C8D",
            command=self._cerrar_tutorial
        ).pack(side="left", padx=6)

        self._tutorial_btn_siguiente = ctk.CTkButton(
            btns_frame, text="Siguiente →", width=130,
            fg_color="#1B3A57", hover_color="#132A3F",
            command=self._siguiente_paso_tutorial
        )
        self._tutorial_btn_siguiente.pack(side="left", padx=6)

        self._tutorial_ventana = win
        win.protocol("WM_DELETE_WINDOW", self._cerrar_tutorial)
        win.grab_set()

    def _siguiente_paso_tutorial(self):
        self._tutorial_paso += 1
        self._mostrar_paso_tutorial()

    def _cerrar_tutorial(self):
        if self._tutorial_ventana is not None and self._tutorial_ventana.winfo_exists():
            self._tutorial_ventana.grab_release()
            self._tutorial_ventana.destroy()
        self._tutorial_ventana = None
        _marcar_tutorial_visto()

    def revisar_cola_escaner(self):
        """Revisa cada 200ms si llegó un código escaneado desde el celular."""
        try:
            while True:
                codigo = codigo_queue.get_nowait()
                self.procesar_codigo_escaneado(codigo)
        except queue.Empty:
            pass
        self.after(200, self.revisar_cola_escaner)

    def procesar_codigo_escaneado(self, codigo):
        """Simula lo que hace el lector físico: mete el código en entry_scan y dispara on_scan()."""
        self.entry_scan.delete(0, "end")
        self.entry_scan.insert(0, codigo)
        self.on_scan()

    def revisar_estado_escaner(self):
        estado = estado_queue.get() if not estado_queue.empty() else None
        if estado == "conectado":
            self._ultimo_ping_escaner = dt.datetime.now()
            self._actualizar_indicador_escaner(True)
        if self._ultimo_ping_escaner and (dt.datetime.now() - self._ultimo_ping_escaner).total_seconds() > 5:
            self._actualizar_indicador_escaner(False)
        self.after(1000, self.revisar_estado_escaner)

    def _actualizar_indicador_escaner(self, conectado):
        if conectado:
            self.lbl_estado_escaner.configure(text="📱 Conectado", text_color="#1F8B4C")
        else:
            self.lbl_estado_escaner.configure(text="📱 Desconectado", text_color="#C0392B")

    def abrir_qr_escaner(self):
        """Abre la ventana con el QR para conectar el celular como escáner."""
        mostrar_ventana_qr(self, self.ip_escaner, self.puerto_escaner)
        

    def _centrar_ventana(self, ancho, alto):
        self.update_idletasks()
        pantalla_ancho = self.winfo_screenwidth()
        pantalla_alto = self.winfo_screenheight()
        x = (pantalla_ancho - ancho) // 2
        y = (pantalla_alto - alto) // 2
        self.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _centrar_toplevel(self, win, ancho, alto):
        self.update_idletasks()
        win.update_idletasks()

        x = self.winfo_x() + (self.winfo_width() - ancho) // 2
        y = self.winfo_y() + (self.winfo_height() - alto) // 2

        pantalla_ancho = win.winfo_screenwidth()
        pantalla_alto = win.winfo_screenheight()
        x = max(0, min(x, pantalla_ancho - ancho))
        y = max(0, min(y, pantalla_alto - alto))

        win.geometry(f"{ancho}x{alto}+{x}+{y}")
    
    def _mostrar_pantalla_carga(self, mensaje):
        ANCHO, ALTO = 360, 140
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Procesando...")
        win.resizable(False, False)
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)

        ctk.CTkLabel(
            win, text=mensaje, font=ctk.CTkFont(size=14, weight="bold")
        ).pack(pady=(28, 15), padx=20)

        barra = ctk.CTkProgressBar(win, mode="indeterminate", width=280)
        barra.pack(pady=(0, 20))
        barra.start()

        win.grab_set()
        win.update_idletasks()
        return win, barra

    def _ejecutar_en_hilo(self, mensaje, trabajo, al_terminar):
        win, barra = self._mostrar_pantalla_carga(mensaje)
        resultado_q = queue.Queue()

        def _worker():
            try:
                resultado = trabajo()
                resultado_q.put((True, resultado))
            except Exception as e:
                resultado_q.put((False, e))

        threading.Thread(target=_worker, daemon=True).start()

        def _revisar():
            try:
                exito, resultado = resultado_q.get_nowait()
            except queue.Empty:
                self.after(50, _revisar)
                return
            barra.stop()
            win.grab_release()
            win.destroy()
            al_terminar(exito, resultado)

        self.after(50, _revisar)

    def cerrar_aplicacion(self):
        salir = messagebox.askyesno(
            "Salir",
            "¿Deseas cerrar la aplicación?\n\n"
            "El progreso ya quedó guardado automáticamente."
        )
        if salir:
            if self._autoguardado_timer is not None:
                self.after_cancel(self._autoguardado_timer)
                self.guardar_sesion()
            _log.info("APP CERRADA")
            for h in _log.handlers:
                h.flush()
            self.destroy()

    def abrir_ayuda(self):
        ANCHO, ALTO = 420, 380
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title(f"Acerca de {APP_NAME}")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.grab_set()

        ctk.CTkLabel(
            win, text=APP_NAME,
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color=COLOR_BRAND_PRIMARY
        ).pack(pady=(30, 5))

        ctk.CTkLabel(
            win, text=f"Versión {VERSION}",
            font=ctk.CTkFont(size=14),
            text_color="gray"
        ).pack()

        sep = ctk.CTkFrame(win, height=1, fg_color="#CCC")
        sep.pack(fill="x", padx=30, pady=15)

        ctk.CTkLabel(
            win, text="Conteo de inventario preciso y veloz\npara almacenes y unidades.",
            font=ctk.CTkFont(size=13),
            justify="center"
        ).pack(pady=(0, 15))

        info_frame = ctk.CTkFrame(win, fg_color="#EDE8DC", corner_radius=10)
        info_frame.pack(padx=25, fill="x", pady=5)

        ctk.CTkLabel(
            info_frame,
            text=("📧 mercedes-almacenm@gmail.com\n"
                  "📞 (618) 231 7387"),
            justify="left",
            font=ctk.CTkFont(size=12), anchor="w"
        ).pack(padx=15, pady=15, fill="x")

        ctk.CTkButton(
            win, text="Entendido", width=100,
            fg_color=COLOR_BRAND_PRIMARY, hover_color="#0D2A45",
            command=win.destroy
        ).pack(pady=(15, 20))           
    
    # ------------------------------------------------------------------
    # Construcción de la interfaz
    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ctk.CTkFrame(self, corner_radius=0, fg_color=COLOR_BRAND_LIGHT)
        top.pack(side="top", fill="x")

        self.lbl_titulo = ctk.CTkLabel(
            top, text="Unidad - Sin Unidad", font=ctk.CTkFont(size=20, weight="bold")
        )
        self.lbl_titulo.pack(side="left", padx=20, pady=(15, 5))

        btn_ayuda = ctk.CTkButton(
            top, text="❓ Ayuda", width=75, height=28,
            fg_color="#A6ACAF", hover_color="#7F8C8D",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.abrir_ayuda
        )
        btn_ayuda.pack(side="right", padx=20, pady=(15, 5))
        crear_tooltip(btn_ayuda, "Información de contacto y versión")

        btn_update = ctk.CTkButton(
            top, text="🔄 Actualizar", width=100, height=28,
            fg_color=COLOR_BRAND_ACCENT, hover_color="#B89430",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._revisar_actualizacion
        )
        btn_update.pack(side="right", pady=(15, 5))
        crear_tooltip(btn_update, "Busca nuevas versiones en GitHub")

        toolbar = ctk.CTkFrame(self, corner_radius=0, fg_color=COLOR_BRAND_LIGHT)
        toolbar.pack(side="top", fill="x")

        grupo_archivo = ctk.CTkFrame(toolbar, fg_color="transparent")
        grupo_archivo.pack(side="left", padx=20, pady=(0, 15))

        btn_cargar = ctk.CTkButton(
            grupo_archivo, text="Cargar Excel", command=self.cargar_excel, width=120
        )
        btn_cargar.pack(side="left", padx=4)
        crear_tooltip(btn_cargar, "Selecciona el archivo .xlsx del inventario")

        self.btn_exportar = ctk.CTkButton(
            grupo_archivo, text="Exportar resultados", command=self.exportar,
            width=150, fg_color="#2FA572", hover_color="#268A5E"
        )
        self.btn_exportar.pack(side="left", padx=4)
        crear_tooltip(self.btn_exportar, "Guarda un Excel con el resumen del conteo")

        btn_escanear = ctk.CTkButton(
            grupo_archivo, text="📷 Escanear con celular", command=self.abrir_qr_escaner,
            width=170, fg_color="#3B82C4", hover_color="#2F6A9E"
        )
        btn_escanear.pack(side="left", padx=4)
        crear_tooltip(btn_escanear, "Abre la cámara del celular como lector de códigos")

        self.lbl_estado_escaner = ctk.CTkLabel(
            grupo_archivo, text="📱 Desconectado", font=ctk.CTkFont(size=11),
            text_color="#C0392B"
        )
        self.lbl_estado_escaner.pack(side="left", padx=(4, 0))

        grupo_peligro = ctk.CTkFrame(toolbar, fg_color="transparent")
        grupo_peligro.pack(side="left", padx=(20, 20), pady=(0, 15))

        btn_nuevo = ctk.CTkButton(
            grupo_peligro, text="Nuevo conteo", command=self.nuevo_conteo, width=110,
            fg_color="#E5533C", hover_color="#C4452F"
        )
        btn_nuevo.pack(side="left", padx=4)
        crear_tooltip(btn_nuevo, "Borra el progreso actual y empieza un conteo nuevo")

        grupo_sesiones = ctk.CTkFrame(toolbar, fg_color="transparent")
        grupo_sesiones.pack(side="right", padx=20, pady=(0, 15))

        btn_guardar = ctk.CTkButton(
            grupo_sesiones, text="Guardar conteo", command=self.guardar_como, width=130,
            fg_color="#8E44AD", hover_color="#6F3589"
        )
        btn_guardar.pack(side="left", padx=4)
        crear_tooltip(btn_guardar, "Guarda el avance del conteo actual con un nombre personalizado")

        btn_abrir = ctk.CTkButton(
            grupo_sesiones, text="Abrir guardado", command=self.abrir_guardado, width=130,
            fg_color="#16A085", hover_color="#12806B"
        )
        btn_abrir.pack(side="left", padx=4)
        crear_tooltip(btn_abrir, "Abre un conteo guardado anteriormente")

        self.banner_ubicacion = ctk.CTkFrame(self, fg_color="#FFF3D6", corner_radius=12)
        self.banner_ubicacion.pack(side="top", fill="x", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            self.banner_ubicacion, text="Ubicación activa:",
            font=ctk.CTkFont(size=14), text_color="#5C4A00"
        ).pack(side="left", padx=(15, 8), pady=10)

        self.lbl_ubicacion_activa = ctk.CTkLabel(
            self.banner_ubicacion, text="Ninguna — escanea una ubicación primero",
            font=ctk.CTkFont(size=16, weight="bold"), text_color="#5C4A00"
        )
        self.lbl_ubicacion_activa.pack(side="left", pady=10)

        stats = ctk.CTkFrame(self, fg_color=COLOR_BRAND_LIGHT, corner_radius=12)
        stats.pack(side="top", fill="x", padx=20, pady=5)

        self.stat_vars = {}
        etiquetas = [
            ("total", "Registros"),
            ("escaneados", "Escaneados"),
            ("coinciden", "Coinciden"),
            ("diferencias", "Con diferencia"),
            ("pendientes", "Pendientes"),
            ("mal_ubicados", "Mal ubicados"),
            ("no_encontrados", "No encontrados"),
        ]
        stat_tooltips = {
            "total": "Haz clic para ver todos los registros",
            "escaneados": "Haz clic para ver solo artículos escaneados",
            "coinciden": "Haz clic para ver solo los que coinciden",
            "diferencias": "Haz clic para ver solo los que tienen diferencia",
            "pendientes": "Haz clic para ver solo los pendientes",
            "mal_ubicados": "Haz clic para ver solo los mal ubicados",
            "no_encontrados": "Haz clic para ver solo los no encontrados",
        }
        for key, label in etiquetas:
            box = ctk.CTkFrame(
                stats, fg_color="#FBF7EE", corner_radius=10, cursor="hand2",
                border_width=1, border_color="#E3DCC8",
            )
            box.pack(side="left", expand=True, fill="x", padx=6, pady=12, ipady=4)
            val = ctk.CTkLabel(box, text="0", font=ctk.CTkFont(size=20, weight="bold"))
            val.pack()
            lbl = ctk.CTkLabel(box, text=label, font=ctk.CTkFont(size=11), text_color="#666666")
            lbl.pack()
            self.stat_vars[key] = val
            self.stat_boxes[key] = box

            categoria = self._categoria_por_stat[key]
            for widget in (box, val, lbl):
                widget.bind("<Button-1>", lambda e, c=categoria: self._filtrar_por_categoria(c))
                crear_tooltip(widget, stat_tooltips[key])

        scan_frame = ctk.CTkFrame(self, fg_color="transparent")
        scan_frame.pack(side="top", fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            scan_frame, text="Escanea aquí:", font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left", padx=(0, 10))

        self.entry_scan = ctk.CTkEntry(
            scan_frame, placeholder_text=f"Ubicación ({PREFIJO_UBICACION}...) o artículo...",
            height=42, font=ctk.CTkFont(size=16)
        )
        self.entry_scan.pack(side="left", fill="x", expand=True)
        self.entry_scan.bind("<Return>", self.on_scan)
        crear_tooltip(self.entry_scan, "Escanear o escribir código de ubicación o artículo")

        ctk.CTkLabel(
            scan_frame, text="Cant.:", font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left", padx=(15, 6))

        self.entry_cantidad = ctk.CTkEntry(
            scan_frame, width=80, height=42, font=ctk.CTkFont(size=16), justify="center"
        )
        self.entry_cantidad.insert(0, "1")
        self.entry_cantidad.pack(side="left")
        self.entry_cantidad.bind("<Return>", lambda e: self.entry_scan.focus_set())
        self.entry_cantidad.bind("<FocusIn>", lambda e: self.entry_cantidad.select_range(0, "end"))
        crear_tooltip(self.entry_cantidad, "Cantidad de unidades a registrar")

        self.lbl_ultimo = ctk.CTkLabel(scan_frame, text="", font=ctk.CTkFont(size=13))
        self.lbl_ultimo.pack(side="left", padx=15)

        self.bind_all("<Button-1>", self._reenfocar_scan, add="+")

        filtro_frame = ctk.CTkFrame(self, fg_color="transparent")
        filtro_frame.pack(side="top", fill="x", padx=20, pady=(0, 5))

        ctk.CTkLabel(filtro_frame, text="Buscar / filtrar:").pack(side="left", padx=(0, 10))
        self.entry_filtro = ctk.CTkEntry(filtro_frame, placeholder_text="Artículo, descripción o ubicación...")
        self.entry_filtro.pack(side="left", fill="x", expand=True)
        self.entry_filtro.bind("<KeyRelease>", lambda e: self._programar_filtro())
        crear_tooltip(self.entry_filtro, "Filtra por nombre, código o descripción")

        self.btn_solo_ubicacion_activa = ctk.CTkButton(
            filtro_frame, text="Solo ubicación activa", width=170,
            fg_color="transparent", border_width=1, border_color="#999999",
            text_color="#333333", hover_color="#EAEAEA",
            command=self._toggle_solo_ubicacion_activa,
        )
        self.btn_solo_ubicacion_activa.pack(side="left", padx=10)
        crear_tooltip(self.btn_solo_ubicacion_activa, "Filtra para mostrar solo artículos de la ubicación actual")
        self.solo_ubicacion_activa = False

        pagination_frame = ctk.CTkFrame(self, fg_color=COLOR_BRAND_LIGHT, corner_radius=0)
        pagination_frame.pack(side="bottom", fill="x", padx=20, pady=(0, 5))

        self.btn_pagina_anterior = ctk.CTkButton(
            pagination_frame, text="← Anterior", width=100, height=28,
            fg_color=COLOR_BRAND_PRIMARY, hover_color="#0D2A45",
            font=ctk.CTkFont(size=11), command=self._pagina_anterior
        )
        self.btn_pagina_anterior.pack(side="left", padx=4)
        crear_tooltip(self.btn_pagina_anterior, "Muestra la página anterior de resultados")

        self.lbl_pagina_info = ctk.CTkLabel(
            pagination_frame, text="",
            font=ctk.CTkFont(size=11), text_color="#666666"
        )
        self.lbl_pagina_info.pack(side="left", padx=10)

        self.btn_pagina_siguiente = ctk.CTkButton(
            pagination_frame, text="Siguiente →", width=100, height=28,
            fg_color=COLOR_BRAND_PRIMARY, hover_color="#0D2A45",
            font=ctk.CTkFont(size=11), command=self._pagina_siguiente
        )
        self.btn_pagina_siguiente.pack(side="left", padx=4)
        crear_tooltip(self.btn_pagina_siguiente, "Muestra la siguiente página de resultados")

        self.lbl_total_registros = ctk.CTkLabel(
            pagination_frame, text="",
            font=ctk.CTkFont(size=11), text_color="#888888"
        )
        self.lbl_total_registros.pack(side="right", padx=10)

        tabla_frame = ctk.CTkFrame(self, fg_color=COLOR_BRAND_LIGHT, corner_radius=12)
        tabla_frame.pack(side="top", fill="both", expand=True, padx=20, pady=(5, 15))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 11),
                         background=COLOR_TABLA_FONDO, fieldbackground=COLOR_TABLA_FONDO,
                         foreground=COLOR_BRAND_PRIMARY)
        style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"),
                         background=COLOR_TABLA_FONDO, foreground=COLOR_BRAND_PRIMARY)
        style.map("Treeview.Heading", background=[("active", COLOR_TABLA_FONDO)])

        cols = ("ubicacion", "articulo", "descripcion", "esperado", "contado", "unidad", "diferencia", "estado")
        self.tree = ttk.Treeview(tabla_frame, columns=cols, show="headings", style="Treeview")
        encabezados = {
            "ubicacion": "Ubicación", "articulo": "Artículo", "descripcion": "Descripción",
            "esperado": "Esperado", "contado": "Contado", "unidad": "Unidad",
            "diferencia": "Diferencia", "estado": "Estado",
        }
        anchos = {"ubicacion": 130, "articulo": 110, "descripcion": 240, "esperado": 80,
                  "contado": 80, "unidad": 70, "diferencia": 90, "estado": 140}
        for c in cols:
            self.tree.heading(c, text=encabezados[c])
            self.tree.column(c, width=anchos[c], anchor="center" if c != "descripcion" else "w")

        scrollbar = ttk.Scrollbar(tabla_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", pady=10)

        self.tree.tag_configure("ok", background=COLOR_OK)
        self.tree.tag_configure("falta", background=COLOR_FALTA)
        self.tree.tag_configure("sobra", background=COLOR_SOBRA)
        self.tree.tag_configure("pendiente", background=COLOR_PENDIENTE)
        self.tree.tag_configure("malubicado", background=COLOR_MAL_UBICADO)
        self.tree.tag_configure("noenc", background=COLOR_NOENC)

        self.tree.bind("<Double-1>", self._editar_registro)

        # ExaStock status bar
        status_bar = ctk.CTkFrame(self, fg_color=COLOR_BRAND_PRIMARY, corner_radius=0)
        status_bar.pack(side="bottom", fill="x", ipady=2)

        ctk.CTkLabel(
            status_bar, text=APP_TITLE,
            font=ctk.CTkFont(size=10), text_color="#8AAEC2"
        ).pack(side="left", padx=12)
        
        self.lbl_status = ctk.CTkLabel(
            status_bar, text="Listo",
            font=ctk.CTkFont(size=10), text_color="#8AAEC2"
        )
        self.lbl_status.pack(side="left", padx=12)

        self.lbl_update_status = ctk.CTkLabel(
            status_bar, text="",
            font=ctk.CTkFont(size=10), text_color="#8AAEC2"
        )
        self.lbl_update_status.pack(side="right", padx=12)

        self.entry_scan.focus_set()

    def _programar_filtro(self):
        """Programa el refresh de la tabla con debounce de 300ms."""
        if self._filtro_timer is not None:
            self.after_cancel(self._filtro_timer)
        self._filtro_timer = self.after(300, self._ejecutar_filtro)

    def _ejecutar_filtro(self):
        self._filtro_timer = None
        self._pagina_actual = 0
        self.refrescar_tabla()

    def _pagina_anterior(self):
        if self._pagina_actual > 0:
            self._pagina_actual -= 1
            self.refrescar_tabla()

    def _pagina_siguiente(self):
        total_paginas = (self._total_filas_filtradas + self._filas_por_pagina - 1) // self._filas_por_pagina
        if self._pagina_actual < total_paginas - 1:
            self._pagina_actual += 1
            self.refrescar_tabla()

    def _actualizar_controles_pagina(self):
        total_paginas = max(1, (self._total_filas_filtradas + self._filas_por_pagina - 1) // self._filas_por_pagina)
        self.lbl_pagina_info.configure(text=f"Página {self._pagina_actual + 1} de {total_paginas}")
        self.lbl_total_registros.configure(text=f"Mostrando {self._total_filas_filtradas} registros")
        self.btn_pagina_anterior.configure(state="normal" if self._pagina_actual > 0 else "disabled")
        self.btn_pagina_siguiente.configure(
            state="normal" if self._pagina_actual < total_paginas - 1 else "disabled"
        )

    def set_status(self, msg):
        if hasattr(self, "lbl_status"):
            self.lbl_status.configure(text=msg)

    def _reenfocar_scan(self, event):
        try:
            widget_str = str(event.widget).lower()
            if "optionmenu" in widget_str or "entry_filtro" in widget_str or "entry_cantidad" in widget_str:
                return
                
            w_class = event.widget.winfo_class()
            if w_class in ("Entry", "Treeview", "Scrollbar", "Canvas", "TMenubutton", "Menu"):
                return
        except Exception:
            pass
        self.entry_scan.focus_set()

    # ------------------------------------------------------------------
    # Carga del Excel
    # ------------------------------------------------------------------
    def _procesar_dataframe(self, df_raw):
        mapa = {}
        duplicadas = {}
        for c in df_raw.columns:
            key = normalizar_columna(c)
            if key in mapa:
                duplicadas.setdefault(key, [mapa[key]]).append(c)
            else:
                mapa[key] = c

        columnas_duplicadas_relevantes = {
            k: v for k, v in duplicadas.items()
            if k in REQUIRED_COLS or k in ["unidaddemedida", "unidadmedida", "unidad", "um", "udm"]
        }
        if columnas_duplicadas_relevantes:
            detalle = "\n".join(
                f"  · {', '.join(cols)}  (se usaría solo \"{mapa[k]}\")"
                for k, cols in columnas_duplicadas_relevantes.items()
            )
            raise ValueError(
                "Hay columnas repetidas en el Excel que apuntan al mismo campo:\n"
                + detalle
                + "\n\nRenombra o elimina las columnas duplicadas y vuelve a intentar."
            )

        faltantes = [c for c in REQUIRED_COLS if c not in mapa]
        if faltantes:
            raise ValueError(
                "No encontré estas columnas en el Excel:\n" + ", ".join(faltantes)
                + "\n\nColumnas detectadas:\n" + ", ".join(df_raw.columns.astype(str))
            )

        df = df_raw.rename(columns={
            mapa["almacen"]: "Almacen", mapa["ubicacion"]: "Ubicacion",
            mapa["articulo"]: "Articulo", mapa["descripcion"]: "Descripcion",
            mapa["existencia"]: "Existencia",
        })

        alias_unidad = ["unidaddemedida", "unidadmedida", "unidad", "um", "udm"]
        col_unidad = next((mapa[a] for a in alias_unidad if a in mapa), None)
        if col_unidad:
            df = df.rename(columns={col_unidad: "Unidad"})
            df["Unidad"] = df["Unidad"].astype(str).str.strip()
        else:
            df["Unidad"] = ""

        df["Articulo"] = df["Articulo"].astype(str).str.strip()
        df["Ubicacion"] = df["Ubicacion"].astype(str).str.strip()
        df["Almacen"] = df["Almacen"].astype(str).str.strip()
        resultados = df["Existencia"].apply(parsear_existencia)
        df["Existencia"] = resultados.apply(lambda r: r[0])
        filas_invalidas = int((~resultados.apply(lambda r: r[1])).sum())
        self._existencias_invalidas = filas_invalidas

        agregado = (
            df.groupby(["Ubicacion", "Articulo"])
            .agg(
                Descripcion=("Descripcion", "first"),
                Existencia=("Existencia", "sum"),
                Almacen=("Almacen", "first"),
                Unidad=("Unidad", "first"),
            )
            .reset_index()
        )
        self._tiene_unidad = col_unidad is not None
        return agregado

    def _indexar(self):
        """Reconstruye los índices auxiliares."""
        self.ubicaciones_norm_map = {}
        self.colisiones_ubicacion = []
        for u in self.df["Ubicacion"].unique():
            u_norm = normalizar(u)
            if u_norm in self.ubicaciones_norm_map and self.ubicaciones_norm_map[u_norm] != u:
                self.colisiones_ubicacion.append((self.ubicaciones_norm_map[u_norm], u))
            else:
                self.ubicaciones_norm_map[u_norm] = u

        self.ubicaciones_set = set(self.ubicaciones_norm_map.keys())
        self.articulos_all_norm = set(normalizar(a) for a in self.df["Articulo"].unique())
        
        self.articulos_norm_map = {normalizar(a): a for a in self.df["Articulo"].unique()}
        
        self.articulos_por_ubicacion = {
            u: set(normalizar(a) for a in grp["Articulo"])
            for u, grp in self.df.groupby("Ubicacion")
        }

    def _avisar_colisiones_ubicacion(self, parent=None):
        if not self.colisiones_ubicacion:
            return
        detalle = "\n".join(f"  · \"{a}\"  vs  \"{b}\"" for a, b in self.colisiones_ubicacion)
        messagebox.showwarning(
            "Ubicaciones parecidas",
            "Hay ubicaciones distintas en el Excel que se ven casi idénticas.\n\n" + detalle,
            parent=parent
        )

    def cargar_excel(self):
        ruta = filedialog.askopenfilename(
            title="Selecciona el reporte de inventario",
            filetypes=[("Excel", "*.xlsx *.xls")]
        )
        if not ruta:
            return

        if self.df is not None:
            progreso = len(self.counts) + len(self.mismatches) + len(self.no_encontrados)
            if progreso > 0:
                if not messagebox.askyesno("Conteo en progreso", "Ya existe un conteo capturado. ¿Deseas perderlo y cargar otro Excel?"):
                    return

        def trabajo():
            df_raw = pd.read_excel(ruta)
            return self._procesar_dataframe(df_raw)

        def al_terminar(exito, resultado):
            if not exito:
                messagebox.showerror("Error", f"No se pudo leer el archivo:\n{resultado}")
                return

            _CACHE_NORMALIZAR.clear()
            self.df = resultado
            self.excel_path = ruta
            self.unidad_actual = str(self.df["Almacen"].iloc[0]) if not self.df.empty else "Sin Unidad"
            self.lbl_titulo.configure(text=f"Unidad - {self.unidad_actual}")
            self.ubicacion_activa = None
            self.archivo_json_activo = None
            self._pagina_actual = 0

            for row in self.tree.get_children():
                self.tree.delete(row)

            self.counts = {}
            self.mismatches = {}
            self.no_encontrados = {}
            self.entry_filtro.delete(0, "end")
            self.solo_ubicacion_activa = False
            self.btn_solo_ubicacion_activa.configure(fg_color="transparent", border_color="#999999")
            self.filtro_categoria = "Todos"
            self._resaltar_stat_activo()
            self._indexar()
            self._actualizar_banner()
            self.guardar_sesion()
            self.refrescar_tabla()
            _log.info("EXCEL CARGADO | %s | %d lineas", os.path.basename(ruta), len(self.df))
            
            aviso_unidad = "" if self._tiene_unidad else "\n\n(No detecté una columna de unidad de medida)"
            aviso_existencias = (
                f"\n\n⚠ {self._existencias_invalidas} fila(s) tenían una existencia no numérica y se dejaron en 0."
                if getattr(self, "_existencias_invalidas", 0) > 0 else ""
            )
            messagebox.showinfo("Listo", f"Se cargaron {len(self.df)} líneas.{aviso_unidad}{aviso_existencias}")
            self._avisar_colisiones_ubicacion()

        self._ejecutar_en_hilo("Leyendo el Excel...", trabajo, al_terminar)

    # ------------------------------------------------------------------
    # Escaneo
    # ------------------------------------------------------------------
    def _progreso_ubicacion(self, ubicacion):
        if self.df is None:
            return 0, 0
        total_ubi = len(self.df[self.df["Ubicacion"] == ubicacion])
        contados = sum(
            1 for k in self.counts
            if k.startswith(ubicacion + SEP)
        )
        return contados, total_ubi

    def _actualizar_banner(self):
        if self.ubicacion_activa:
            contados, total = self._progreso_ubicacion(self.ubicacion_activa)
            texto = f"{self.ubicacion_activa}  ·  {contados}/{total} artículos contados"
            self.lbl_ubicacion_activa.configure(text=texto)
            self.banner_ubicacion.configure(fg_color="#DCEEFF")
            self.lbl_ubicacion_activa.configure(text_color="#0B4C8C")
        else:
            self.lbl_ubicacion_activa.configure(text="Ninguna — escanea una ubicación primero")
            self.banner_ubicacion.configure(fg_color="#FFF3D6")
            self.lbl_ubicacion_activa.configure(text_color="#5C4A00")

    def _leer_cantidad(self):
        texto = self.entry_cantidad.get().strip().replace(",", ".")
        try:
            valor = float(texto)
            return valor if valor > 0 else 1
        except (TypeError, ValueError):
            return 1

    def _reset_cantidad(self):
        self.entry_cantidad.delete(0, "end")
        self.entry_cantidad.insert(0, "1")

    def _actualizar_fila_escaneada(self, key, articulo, descripcion):
        """Actualiza solo la fila escaneada en el Treeview sin refrescar toda la tabla."""
        for item_id in self.tree.get_children():
            valores = self.tree.item(item_id, "values")
            if len(valores) >= 2 and valores[1] == articulo and valores[0] == self.ubicacion_activa:
                esperado = float(valores[3]) if valores[3] != "-" else 0
                contado = self.counts.get(key, 0)
                tag, estado = self._estado_fila(esperado, contado)
                diferencia = contado - esperado
                self.tree.item(item_id, values=(
                    valores[0], valores[1], valores[2], fmt_num(esperado),
                    fmt_num(contado), valores[5], fmt_num(diferencia), estado
                ), tags=(tag,))
                return
        self.refrescar_tabla()

    def _actualizar_stats_escalera(self):
        """Actualiza las estadísticas sin refrescar la tabla completa."""
        if self.df is None:
            return
        total = len(self.df)

        keys = [clave(row.Ubicacion, row.Articulo) for row in self.df.itertuples(index=False)]
        contados = [self.counts.get(k, 0) for k in keys]

        diferencias = [c - e for c, e in zip(contados, self.df["Existencia"])]
        n_coinciden = sum(1 for d in diferencias if abs(d) < TOLERANCIA)
        n_pendientes = sum(1 for c, d in zip(contados, diferencias) if c == 0 and abs(d) >= TOLERANCIA)
        n_diferencias = total - n_coinciden - n_pendientes

        self._actualizar_stats(
            total, len(self.counts), n_coinciden, n_diferencias, n_pendientes,
            len(self.mismatches), len(self.no_encontrados)
        )

    def on_scan(self, event=None):
        codigo_raw = self.entry_scan.get().strip()
        self.entry_scan.delete(0, "end")
        if not codigo_raw:
            return

        if self.df is None:
            messagebox.showwarning("Sin reporte", "Primero carga el Excel del reporte.")
            return

        cantidad = self._leer_cantidad()
        codigo_norm = normalizar(codigo_raw)
        prefijo_norm = normalizar(PREFIJO_UBICACION)

        if codigo_norm.startswith(prefijo_norm):
            sin_prefijo = codigo_norm[len(prefijo_norm):].lstrip("-_ ").strip()
            target = codigo_norm if codigo_norm in self.ubicaciones_set else (sin_prefijo if sin_prefijo in self.ubicaciones_set else None)

            if target is None:
                sonido_error()
                self.lbl_ultimo.configure(text=f"⚠ '{codigo_raw}' no está en el reporte", text_color="#C0392B")
                return

            self.ubicacion_activa = self.ubicaciones_norm_map[target]
            self._actualizar_banner()
            self.lbl_ultimo.configure(text=f"📍 Ubicación activa: {self.ubicacion_activa}", text_color="#0B4C8C")
            sonido_ok()
            self._reset_cantidad()
            self._programar_autoguardado()
            _log.info("UBICACION | %s", self.ubicacion_activa)
            self._pagina_actual = 0
            self.refrescar_tabla()
            return

        if codigo_norm in self.ubicaciones_set:
            self.ubicacion_activa = self.ubicaciones_norm_map[codigo_norm]
            self._actualizar_banner()
            self.lbl_ultimo.configure(text=f"📍 Ubicación activa: {self.ubicacion_activa}", text_color="#0B4C8C")
            sonido_ok()
            self._reset_cantidad()
            self._programar_autoguardado()
            _log.info("UBICACION | %s", self.ubicacion_activa)
            self._pagina_actual = 0
            self.refrescar_tabla()
            return

        if self.ubicacion_activa is None:
            sonido_error()
            self.lbl_ultimo.configure(text="⚠ Escanea primero una ubicación válida", text_color="#C0392B")
            return

        articulos_aqui = self.articulos_por_ubicacion.get(self.ubicacion_activa, set())
        if codigo_norm in articulos_aqui:
            articulo_original = self._buscar_articulo_original(self.ubicacion_activa, codigo_norm)
            k = clave(self.ubicacion_activa, articulo_original)
            self.counts[k] = self.counts.get(k, 0) + cantidad
            desc = self.df.loc[(self.df["Ubicacion"] == self.ubicacion_activa) & (self.df["Articulo"] == articulo_original), "Descripcion"].iloc[0]
            sonido_ok()
            _log.info("SCAN OK | %s | %s x%s | total=%s", self.ubicacion_activa, articulo_original, fmt_num(cantidad), fmt_num(self.counts[k]))
            self.lbl_ultimo.configure(text=f"✔ {articulo_original} (van {fmt_num(self.counts[k])})", text_color="#1F8B4C")
            self._actualizar_fila_escaneada(k, articulo_original, desc)

        elif codigo_norm in self.articulos_all_norm:
            articulo_original = self._buscar_articulo_original_global(codigo_norm)
            k = clave(self.ubicacion_activa, articulo_original)
            self.mismatches[k] = self.mismatches.get(k, 0) + cantidad
            ubic_correctas = self.df.loc[self.df["Articulo"] == articulo_original, "Ubicacion"].unique()
            sonido_alerta()
            _log.info("MAL UBICADO | %s en %s (deberia ir en %s)", articulo_original, self.ubicacion_activa, ", ".join(ubic_correctas))
            self.lbl_ultimo.configure(
                text=f"✘ {articulo_original} no va aquí (va en: {', '.join(ubic_correctas)})", text_color="#A83279"
            )
            self._actualizar_fila_escaneada(k, articulo_original, desc)

        else:
            if codigo_norm in self.no_encontrados:
                self.no_encontrados[codigo_norm]["veces"] += cantidad
            else:
                self.no_encontrados[codigo_norm] = {"veces": cantidad, "texto": codigo_raw}
            sonido_error()
            _log.info("NO ENCONTRADO | %s en %s", codigo_raw, self.ubicacion_activa)
            self.lbl_ultimo.configure(text=f"✘ {codigo_raw} no está en el reporte", text_color="#C0392B")

        self._reset_cantidad()
        self._programar_autoguardado()
        self._actualizar_banner()
        self._actualizar_stats_escalera()

    def _buscar_articulo_original(self, ubicacion, articulo_norm):
        sub = self.df[self.df["Ubicacion"] == ubicacion]
        for a in sub["Articulo"]:
            if normalizar(a) == articulo_norm:
                return a
        return articulo_norm

    def _buscar_articulo_original_global(self, articulo_norm):
        return self.articulos_norm_map.get(articulo_norm, articulo_norm)

    def _descripcion_articulo(self, articulo):
        fila = self.df.loc[self.df["Articulo"] == articulo, "Descripcion"]
        return fila.iloc[0] if not fila.empty else "-"

    def _grid_articulo_unidad(self, articulo):
        fila = self.df.loc[self.df["Articulo"] == articulo, "Unidad"]
        return fila.iloc[0] if not fila.empty and fila.iloc[0] else "-"

    # ------------------------------------------------------------------
    # Edición manual de un registro de la tabla
    # ------------------------------------------------------------------
    def _editar_registro(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        valores = self.tree.item(item_id, "values")
        if not valores:
            return
        ubicacion, articulo, descripcion, esperado, contado, unidad, diferencia, estado = valores

        if estado in ("OK", "Falta", "Sobra", "Pendiente"):
            tipo = "normal"
            key = clave(ubicacion, articulo)
            actual = self.counts.get(key, 0)
            titulo = f"{articulo} — {descripcion}\n({ubicacion})"
            self._abrir_dialogo_edicion(tipo, key, actual, titulo)
        elif estado == "Mal ubicado":
            tipo = "mismatch"
            key = None
            for k in self.mismatches.keys():
                u, a = declave(k)
                if normalizar(u) == normalizar(ubicacion) and normalizar(a) == normalizar(articulo):
                    key = k
                    break
            if not key:
                key = clave(ubicacion, articulo)
                
            actual = self.mismatches.get(key, 0)
            titulo = f"{articulo} — {descripcion}\n(mal ubicado en {ubicacion})"
            self._abrir_dialogo_edicion(tipo, key, actual, titulo)
        elif estado == "No encontrado":
            tipo = "noenc"
            key = normalizar(articulo)
            actual = self.no_encontrados.get(key, {}).get("veces", 0)
            titulo = f"Artículo No Registrado:\n{articulo}"
            self._abrir_dialogo_edicion(tipo, key, actual, titulo, ubicacion_actual=ubicacion, desc_actual=descripcion)

    def _ajustar_texto(self, texto, ancho_caracteres=42):
        lineas = []
        for parte in str(texto).split("\n"):
            if parte.strip():
                lineas.extend(textwrap.wrap(
                    parte, width=ancho_caracteres, break_long_words=True, break_on_hyphens=False
                ))
            else:
                lineas.append("")
        return "\n".join(lineas), len(lineas)

    def _abrir_dialogo_edicion(self, tipo, key, actual, titulo, ubicacion_actual="", desc_actual=""):
        ANCHO = 400
        ALTO = 380 if tipo == "noenc" else 240
        
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Editar registro contado")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)
        win.grab_set()

        titulo_ajustado, n_lineas = self._ajustar_texto(titulo, ancho_caracteres=42)
        ctk.CTkLabel(
            win, text=titulo_ajustado, font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=ANCHO - 40, justify="center"
        ).pack(pady=(15, 10), padx=20)

        # --- CAMPOS NORMALES (Solo cantidad) ---
        if tipo != "noenc":
            ctk.CTkLabel(win, text="Nueva cantidad contada:").pack(pady=(0, 5))
            entry_cant = ctk.CTkEntry(win, width=140, height=35, justify="center", font=ctk.CTkFont(size=16))
            entry_cant.insert(0, str(fmt_num(actual)))
            entry_cant.pack(pady=5)
            entry_cant.focus_set()
            entry_cant.select_range(0, "end")
        
        # --- CAMPOS PARA NO ENCONTRADOS (Artículo, Ubicación, Descripción, Cantidad) ---
        else:
            frame_campos = ctk.CTkFrame(win, fg_color="transparent")
            frame_campos.pack(padx=20, fill="x", pady=5)
            frame_campos.columnconfigure(1, weight=1)

            # Campo: Código de Artículo
            ctk.CTkLabel(frame_campos, text="Artículo / Código:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 10))
            entry_art = ctk.CTkEntry(frame_campos, height=30)
            entry_art.insert(0, str(key if not hasattr(self, "articulos_norm_map") or key not in self.articulos_norm_map else self.articulos_norm_map[key]))
            # Si el elemento tiene un texto personalizado en el diccionario, cargamos ese (que conserva mayúsculas)
            if key in self.no_encontrados and "texto" in self.no_encontrados[key]:
                entry_art.delete(0, "end")
                entry_art.insert(0, str(self.no_encontrados[key]["texto"]))
            entry_art.grid(row=0, column=1, sticky="ew", pady=4)

            # Campo: Ubicación manual
            ctk.CTkLabel(frame_campos, text="Ubicación:").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
            entry_ubi = ctk.CTkEntry(frame_campos, height=30)
            entry_ubi.insert(0, str(ubicacion_actual if ubicacion_actual != "-" else ""))
            entry_ubi.grid(row=1, column=1, sticky="ew", pady=4)

            # Campo: Descripción manual
            ctk.CTkLabel(frame_campos, text="Descripción:").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 10))
            entry_desc = ctk.CTkEntry(frame_campos, height=30)
            entry_desc.insert(0, str(desc_actual if "No existe en el reporte" not in desc_actual else ""))
            entry_desc.grid(row=2, column=1, sticky="ew", pady=4)

            # Campo: Cantidad
            ctk.CTkLabel(frame_campos, text="Cantidad:").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 10))
            entry_cant = ctk.CTkEntry(frame_campos, height=30, justify="center")
            entry_cant.insert(0, str(fmt_num(actual)))
            entry_cant.grid(row=3, column=1, sticky="w", pady=4, ipadx=20)
            
            entry_cant.focus_set()
            entry_cant.select_range(0, "end")

        def guardar(_event=None):
            texto_cant = entry_cant.get().strip().replace(",", ".")
            try:
                nuevo_cant = float(texto_cant)
                if nuevo_cant < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Valor inválido", "Escribe un número de cantidad mayor o igual a 0.", parent=win)
                return

            if tipo == "noenc":
                nuevo_art = entry_art.get().strip()
                nuevo_ubi = entry_ubi.get().strip()
                nuevo_desc = entry_desc.get().strip()

                if not nuevo_art:
                    messagebox.showerror("Falta código", "El código del artículo no puede quedar vacío.", parent=win)
                    return

                # Borrar clave anterior para evitar duplicados
                self.no_encontrados.pop(key, None)

                if nuevo_cant > 0:
                    nueva_clave = normalizar(nuevo_art)
                    self.no_encontrados[nueva_clave] = {
                        "veces": nuevo_cant,
                        "texto": nuevo_art,
                        "ubicacion_manual": nuevo_ubi if nuevo_ubi else "-",
                        "descripcion_manual": nuevo_desc if nuevo_desc else "— No existe en el reporte (Editado) —"
                    }
            else:
                destino = {"normal": self.counts, "mismatch": self.mismatches}[tipo]
                if nuevo_cant == 0:
                    destino.pop(key, None)
                else:
                    destino[key] = nuevo_cant

            self._programar_autoguardado()
            self._actualizar_banner()
            _log.info("EDICION | tipo=%s | key=%s | cantidad=%s", tipo, key, fmt_num(nuevo_cant))
            self.refrescar_tabla()
            win.destroy()

        if tipo != "noenc":
            entry_cant.bind("<Return>", guardar)

        btns = ctk.CTkFrame(win, fg_color="transparent", width=260, height=45)
        btns.pack(pady=(15, 10))
        btns.pack_propagate(False)
        ctk.CTkButton(btns, text="Guardar", command=guardar, width=110, fg_color="#2FA572", hover_color="#268A5E").pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Cancelar", command=win.destroy, width=110, fg_color="#E5533C", hover_color="#777777").pack(side="left", padx=8)

    # ------------------------------------------------------------------
    # Tabla y estadísticas
    # ------------------------------------------------------------------
    def _estado_fila(self, esperado, contado):
        diferencia = contado - esperado
        if abs(diferencia) < TOLERANCIA:
            return "ok", "OK"
        if contado == 0:
            return "pendiente", "Pendiente"
        if diferencia < 0:
            return "falta", "Falta"
        return "sobra", "Sobra"

    def _toggle_solo_ubicacion_activa(self):
        self.solo_ubicacion_activa = not self.solo_ubicacion_activa
        activo = self.solo_ubicacion_activa
        self.btn_solo_ubicacion_activa.configure(
            fg_color="#E4ECFB" if activo else "transparent",
            border_color="#3B82F6" if activo else "#999999",
        )
        self._pagina_actual = 0
        self.refrescar_tabla()

    def _filtrar_por_categoria(self, categoria):
        self.filtro_categoria = categoria
        self._pagina_actual = 0
        self._resaltar_stat_activo()
        self.refrescar_tabla()

    def _resaltar_stat_activo(self):
        for key, box in self.stat_boxes.items():
            activo = self._categoria_por_stat[key] == self.filtro_categoria
            if activo:
                box.configure(fg_color="#E4ECFB", border_color="#B7CCF0")
            else:
                box.configure(fg_color="#FBF7EE", border_color="#E3DCC8")

    def refrescar_tabla(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        if self.df is None:
            self._actualizar_stats(0, 0, 0, 0, 0, 0, 0)
            self._total_filas_filtradas = 0
            self._actualizar_controles_pagina()
            return

        filtro_texto = normalizar(self.entry_filtro.get())
        solo_ubicacion_activa = self.solo_ubicacion_activa
        categoria = self.filtro_categoria

        total = len(self.df)
        n_coinciden = n_diferencias = n_pendientes = 0
        filas_visibles = []
        n_mal_ubicados = 0
        n_no_encontrados = 0

        for fila in self.df.itertuples(index=False):
            ubicacion = fila.Ubicacion
            articulo = fila.Articulo
            esperado = fila.Existencia
            k = clave(ubicacion, articulo)
            contado = self.counts.get(k, 0)
            tag, estado = self._estado_fila(esperado, contado)

            if estado == "OK":
                n_coinciden += 1
            elif estado == "Pendiente":
                n_pendientes += 1
            else:
                n_diferencias += 1

            if solo_ubicacion_activa and ubicacion != self.ubicacion_activa:
                continue
            if categoria == "Pendientes" and estado != "Pendiente":
                continue
            if categoria == "Coinciden" and estado != "OK":
                continue
            if categoria == "Con diferencia" and estado not in ("Falta", "Sobra"):
                continue
            if categoria == "Escaneados" and k not in self.counts:
                continue
            if categoria in ("Mal ubicados", "No encontrados"):
                continue

            if filtro_texto and not any(
                filtro_texto in normalizar(v) for v in (articulo, fila.Descripcion, ubicacion)
            ):
                continue

            diferencia = contado - esperado
            filas_visibles.append((
                ubicacion, articulo, fila.Descripcion, fmt_num(esperado), fmt_num(contado),
                getattr(fila, 'Unidad', None) or "-", fmt_num(diferencia), estado, tag
            ))

        n_mal_ubicados = len(self.mismatches)
        if categoria in ("Todos", "Escaneados", "Mal ubicados"):
            for k, veces in self.mismatches.items():
                ubic, art = declave(k)
                if solo_ubicacion_activa and ubic != self.ubicacion_activa:
                    continue
                if filtro_texto and not any(filtro_texto in normalizar(v) for v in (art, ubic)):
                    continue
                desc_art = self._descripcion_articulo(art)
                unidad_art = self._grid_articulo_unidad(art)
                filas_visibles.append((
                    ubic, art, desc_art, "-", fmt_num(veces), unidad_art, "-", "Mal ubicado", "malubicado"
                ))

        if not solo_ubicacion_activa and categoria in ("Todos", "Escaneados", "No encontrados"):
            for codigo_norm, info in self.no_encontrados.items():
                texto = info["texto"]
                veces = info["veces"]
                ubi_mostrar = info.get("ubicacion_manual", "-")
                desc_mostrar = info.get("descripcion_manual", "— No existe en el reporte —")

                if filtro_texto and not any(filtro_texto in normalizar(v) for v in (codigo_norm, ubi_mostrar, desc_mostrar)):
                    continue
                filas_visibles.append((
                    ubi_mostrar, texto, desc_mostrar, 0, fmt_num(veces), "-", fmt_num(veces), "No encontrado", "noenc"
                ))

        self._total_filas_filtradas = len(filas_visibles)
        inicio = self._pagina_actual * self._filas_por_pagina
        fin = inicio + self._filas_por_pagina

        for fila in filas_visibles[inicio:fin]:
            ubicacion, articulo, desc, esperado, contado, unidad, diferencia, estado, tag = fila
            self.tree.insert("", "end", values=(
                ubicacion, articulo, desc, esperado, contado, unidad, diferencia, estado
            ), tags=(tag,))

        self._actualizar_stats(
            total, len(self.counts), n_coinciden, n_diferencias, n_pendientes,
            n_mal_ubicados, len(self.no_encontrados)
        )
        self._actualizar_controles_pagina()

    def _actualizar_stats(self, total, escaneados, coinciden, diferencias, pendientes, mal_ubicados, no_encontrados):
        self.stat_vars["total"].configure(text=str(total))
        self.stat_vars["escaneados"].configure(text=str(escaneados))
        self.stat_vars["coinciden"].configure(text=str(coinciden))
        self.stat_vars["diferencias"].configure(text=str(diferencias))
        self.stat_vars["pendientes"].configure(text=str(pendientes))
        self.stat_vars["mal_ubicados"].configure(text=str(mal_ubicados))
        self.stat_vars["no_encontrados"].configure(text=str(no_encontrados))

    # ------------------------------------------------------------------
    # Persistencia de sesión
    # ------------------------------------------------------------------
    def _construir_datos_sesion(self, nombre=None):
        return {
            "nombre": nombre,
            "excel_path": self.excel_path,
            "ubicacion_activa": self.ubicacion_activa,
            "counts": self.counts,
            "mismatches": self.mismatches,
            "no_encontrados": self.no_encontrados,
            "guardado": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
        }

    def _programar_autoguardado(self):
        """Programa el autoguardado con debounce de 2 segundos.
        Cada llamada cancela la anterior, evitando escrituras excesivas a disco."""
        if self._autoguardado_timer is not None:
            self.after_cancel(self._autoguardado_timer)
        self._autoguardado_timer = self.after(2000, self._ejecutar_autoguardado)

    def _ejecutar_autoguardado(self):
        self._autoguardado_timer = None
        self.guardar_sesion()

    def guardar_sesion(self):
        if self.df is None:
            return
        data = self._construir_datos_sesion()
        tmp_path = SESSION_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, SESSION_FILE)
        except Exception:
            pass

    def _revisar_sesion_previa(self):
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        ruta = data.get("excel_path")
        if not ruta or not os.path.exists(ruta):
            return

        continuar = messagebox.askyesno(
            "Conteo pendiente",
            f"Hay un conteo sin terminar de:\n{os.path.basename(ruta)}\n¿Quieres continuarlo?"
        )
        if not continuar:
            return

        self._cargar_datos_en_app(data)

    def _migrar_no_encontrados(self, no_encontrados):
        migrado = {}
        for k, v in no_encontrados.items():
            if isinstance(v, dict):
                migrado[k] = v
            else:
                k_norm = normalizar(k)
                if k_norm in migrado:
                    migrado[k_norm]["veces"] += v
                else:
                    migrado[k_norm] = {"veces": v, "texto": k}
        return migrado

    def _cargar_datos_en_app(self, data, parent=None):
        ruta_excel = data.get("excel_path")
        if not ruta_excel or not os.path.exists(ruta_excel):
            messagebox.showwarning("Excel no encontrado", "No encuentro el Excel original. Selecciónalo.", parent=parent)
            ruta_excel = filedialog.askopenfilename(title="Selecciona el Excel", filetypes=[("Excel", "*.xlsx *.xls")], parent=parent)
            if not ruta_excel:
                return False

        try:
            df_raw = pd.read_excel(ruta_excel)
            self.df = self._procesar_dataframe(df_raw)
            self.excel_path = ruta_excel
            self.unidad_actual = str(self.df["Almacen"].iloc[0]) if not self.df.empty else "Sin Unidad"
            self.lbl_titulo.configure(text=f"Unidad - {self.unidad_actual}")
            self._indexar()
            self.ubicacion_activa = data.get("ubicacion_activa")
            self.counts = data.get("counts", {})
            self.mismatches = data.get("mismatches", {})
            self.no_encontrados = self._migrar_no_encontrados(data.get("no_encontrados", {}))
            self._actualizar_banner()
            self.guardar_sesion()
            self.refrescar_tabla()
            _log.info("SESION CARGADA | %s | excel=%s", data.get("nombre", "-"), os.path.basename(ruta_excel))
            self._avisar_colisiones_ubicacion(parent=parent)
            return True
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo cargar el conteo:\n{e}", parent=parent)
            return False

    def _nombre_archivo_seguro(self, nombre):
        limpio = "".join(c for c in nombre if c.isalnum() or c in (" ", "-", "_")).strip()
        return limpio or dt.datetime.now().strftime("conteo_%Y%m%d_%H%M%S")

    def guardar_como(self):
        if self.df is None:
            messagebox.showwarning("Sin datos", "Primero carga un reporte.")
            return
        self._abrir_dialogo_nombre_conteo()

    def _abrir_dialogo_nombre_conteo(self):
        ANCHO, ALTO = 420, 230
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Guardar conteo")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)
        win.grab_set()

        ctk.CTkLabel(win, text="Guardar conteo", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(25, 5))
        ctk.CTkLabel(win, text="Nombre para este conteo:").pack(pady=(0, 8))

        entry = ctk.CTkEntry(win, width=300, height=38, font=ctk.CTkFont(size=14), justify="center")
        entry.pack(pady=5)
        entry.focus_set()

        def guardar(_event=None):
            nombre = entry.get().strip()
            if not nombre:
                messagebox.showerror("Falta el nombre", "Escribe un nombre para el conteo.", parent=win)
                return
            win.destroy()
            self._guardar_conteo_con_nombre(nombre)

        entry.bind("<Return>", guardar)

        btns = ctk.CTkFrame(win, fg_color="transparent", width=260, height=50)
        btns.pack(pady=15)
        btns.pack_propagate(False)
        ctk.CTkButton(btns, text="Guardar", command=guardar, width=110, fg_color="#2FA572", hover_color="#268A5E").pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Cancelar", command=win.destroy, width=110, fg_color="#E5533C", hover_color="#777777").pack(side="left", padx=8)

    def _guardar_conteo_con_nombre(self, nombre):
        nombre_archivo = self._nombre_archivo_seguro(nombre)
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        ruta = os.path.join(SESSIONS_DIR, nombre_archivo + ".json")
        if os.path.exists(ruta):
            if not messagebox.askyesno("Ya existe", f"¿Sobrescribir \"{nombre_archivo}\"?"):
                return

        data = self._construir_datos_sesion(nombre=nombre.strip())
        tmp_path = ruta + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, ruta)
            self.archivo_json_activo = os.path.abspath(ruta)  # Enlazar como archivo activo
            _log.info("GUARDADO CON NOMBRE | %s", nombre.strip())
            messagebox.showinfo("Guardado", f"Conteo guardado como \"{nombre.strip()}\".")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo guardar:\n{e}")

    def abrir_guardado(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        archivos = sorted(
            (f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(SESSIONS_DIR, f)),
            reverse=True,
        )
        if not archivos:
            messagebox.showinfo("Sin conteos guardados", "Todavía no has guardado ningún conteo.")
            return
        self._abrir_selector_sesiones(archivos)

    def _leer_resumen_sesion(self, ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        progreso = len(data.get("counts", {})) + len(data.get("mismatches", {})) + len(data.get("no_encontrados", {}))
        return {
            "nombre": data.get("nombre") or os.path.splitext(os.path.basename(ruta))[0],
            "excel_nombre": os.path.basename(data["excel_path"]) if data.get("excel_path") else "-",
            "guardado": data.get("guardado", "-"),
            "progreso": progreso,
        }

    def _abrir_selector_sesiones(self, archivos):
        ANCHO, ALTO = 580, 440
        win = ctk.CTkToplevel(self)
        win.configure(fg_color=COLOR_BRAND_LIGHT)
        win.title("Conteos guardados")
        win.resizable(False, False)
        win.transient(self)
        self._centrar_toplevel(win, ANCHO, ALTO)
        win.minsize(ANCHO, ALTO)
        win.maxsize(ANCHO, ALTO)
        win.grab_set()

        ctk.CTkLabel(win, text="Selecciona un conteo para continuar", font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(15, 10))

        scroll = ctk.CTkScrollableFrame(win, width=530, height=320)
        scroll.pack(padx=15, pady=5, fill="both", expand=True)

        ANCHO_BOTONES = 190

        for archivo in archivos:
            ruta = os.path.join(SESSIONS_DIR, archivo)
            info = self._leer_resumen_sesion(ruta)
            if info is None:
                continue

            fila = ctk.CTkFrame(scroll, fg_color=COLOR_BRAND_LIGHT, corner_radius=10)
            fila.pack(fill="x", pady=5, padx=5)

            nombre, _ = self._ajustar_texto(info['nombre'], ancho_caracteres=34)
            excel_nombre, _ = self._ajustar_texto(info['excel_nombre'], ancho_caracteres=34)
            texto = f"{nombre}\nExcel: {excel_nombre}\nGuardado: {info['guardado']} · {info['progreso']} escaneados"
            
            ctk.CTkLabel(
                fila, text=texto, justify="left", font=ctk.CTkFont(size=12), anchor="w",
                wraplength=530 - ANCHO_BOTONES - 40
            ).pack(side="left", padx=12, pady=10, fill="x", expand=True)

            btns = ctk.CTkFrame(fila, fg_color="transparent", width=ANCHO_BOTONES, height=44)
            btns.pack(side="right", padx=10)
            btns.pack_propagate(False)

            ctk.CTkButton(btns, text="Abrir", width=80, command=lambda r=ruta, w=win: self._elegir_sesion_guardada(r, w)).pack(side="left", padx=4)
            ctk.CTkButton(btns, text="Eliminar", width=80, fg_color="#E5533C", hover_color="#C4452F", command=lambda r=ruta, w=win: self._eliminar_sesion_guardada(r, w)).pack(side="left", padx=4)

    def _elegir_sesion_guardada(self, ruta, win):
        nombre_conteo = os.path.splitext(os.path.basename(ruta))[0]

        # Validar si el archivo seleccionado ya está activo
        if self.archivo_json_activo == os.path.abspath(ruta):
            messagebox.showinfo(
                "Ya abierto", 
                f"El conteo \"{nombre_conteo}\" ya se encuentra abierto y activo en la aplicación.",
                parent=win
            )
            return

        # Solicitar confirmación antes de abrir
        confirmar = messagebox.askyesno(
            "Confirmar apertura",
            f"¿Estás seguro de que deseas abrir el conteo \"{nombre_conteo}\"?\n\n"
            "Si tienes un progreso actual sin guardar con nombre, asegúrate de guardarlo primero.",
            parent=win
        )
        
        if not confirmar:
            return

        try:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo abrir el archivo:\n{e}", parent=win)
            return

        if self._cargar_datos_en_app(data, parent=win):
            self.archivo_json_activo = os.path.abspath(ruta)  # Registrar ruta absoluta activa
            win.destroy()
            messagebox.showinfo("Listo", f"Continuando el conteo \"{data.get('nombre', '-')}\".")

    def _eliminar_sesion_guardada(self, ruta, win):
        if not messagebox.askyesno("Eliminar", "¿Eliminar este conteo?", parent=win):
            return
        try:
            os.remove(ruta)
            if self.archivo_json_activo == os.path.abspath(ruta):
                self.archivo_json_activo = None
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo eliminar:\n{e}", parent=win)
            return
        win.destroy()
        self.abrir_guardado()

    # ------------------------------------------------------------------
    # Nuevo conteo / exportar
    # ------------------------------------------------------------------
    def nuevo_conteo(self):
        if self.df is None:
            return
        if not messagebox.askyesno("Confirmar", "Esto borrará el progreso actual del conteo. ¿Continuar?"):
            return
        self.ubicacion_activa = None
        self.archivo_json_activo = None
        self.counts = {}
        self.mismatches = {}
        self.no_encontrados = {}
        self._actualizar_banner()
        self._programar_autoguardado()
        _log.info("NUEVO CONTEO | progreso anterior borrado")
        self.refrescar_tabla()

    def exportar(self):
        if self.df is None:
            messagebox.showwarning("Sin datos", "Primero carga un reporte y realiza el conteo.")
            return

        ruta = filedialog.asksaveasfilename(
            title="Guardar resultados",
            defaultextension=".xlsx",
            initialdir=BASE_DIR,
            initialfile=f"resultado_conteo_{dt.date.today().isoformat()}.xlsx",
            filetypes=[("Excel", "*.xlsx")],
        )
        if not ruta:
            return

        df_snapshot = self.df
        counts_snapshot = dict(self.counts)
        mismatches_snapshot = dict(self.mismatches)
        no_encontrados_snapshot = dict(self.no_encontrados)

        def trabajo():
            filas = []
            for _, fila in df_snapshot.iterrows():
                ubicacion = fila["Ubicacion"]
                articulo = fila["Articulo"]
                esperado = fila["Existencia"]
                k = clave(ubicacion, articulo)
                contado = counts_snapshot.get(k, 0)
                _, estado = self._estado_fila(esperado, contado)
                filas.append({
                    "Almacén": fila["Almacen"], "Ubicación": ubicacion, "Artículo": articulo,
                    "Descripción": fila["Descripcion"], "Unidad": fila["Unidad"] or "-",
                    "Existencia esperada": fmt_num(esperado), "Cantidad contada": fmt_num(contado),
                    "Diferencia": fmt_num(contado - esperado), "Estado": estado,
                })

            for k, veces in mismatches_snapshot.items():
                ubic, art = declave(k)
                filas.append({
                    "Almacén": "-", "Ubicación": ubic, "Artículo": art,
                    "Descripción": self._descripcion_articulo(art), "Unidad": self._grid_articulo_unidad(art),
                    "Existencia esperada": "-", "Cantidad contada": fmt_num(veces),
                    "Diferencia": "-", "Estado": "Mal ubicado",
                })

            for codigo_norm, info in no_encontrados_snapshot.items():
                texto = info["texto"]
                veces = info["veces"]
                ubi_manual = info.get("ubicacion_manual", "-")
                desc_manual = info.get("descripcion_manual", "No existe en el reporte")

                filas.append({
                    "Almacén": "-", 
                    "Ubicación": ubi_manual, 
                    "Artículo": texto,
                    "Descripción": desc_manual, 
                    "Unidad": "-", 
                    "Existencia esperada": 0,
                    "Cantidad contada": fmt_num(veces), 
                    "Diferencia": fmt_num(veces), 
                    "Estado": "No encontrado",
                })

            resultado = pd.DataFrame(filas)
            with pd.ExcelWriter(ruta, engine="openpyxl") as writer:
                resultado.to_excel(writer, index=False, sheet_name="Resultado conteo")
            return ruta

        def al_terminar(exito, resultado):
            if not exito:
                messagebox.showerror("Error", f"No se pudo guardar el archivo:\n{resultado}")
                return
            _log.info("EXPORTADO | %s", resultado)
            messagebox.showinfo("Exportado", f"Resultados guardados en:\n{resultado}")

        self._ejecutar_en_hilo("Exportando resultados...", trabajo, al_terminar)


if __name__ == "__main__":
    app = InventarioApp()
    app.mainloop()