"""
Módulo: mobile_scanner.py
--------------------------
Convierte el celular en un lector de código de barras inalámbrico para
la app "Conteo de Inventario".

Requiere:
    pip install flask
    pip install qrcode[pil]   (opcional, para mostrar el QR de conexión)

Uso dentro de tu app CustomTkinter:

    from mobile_scanner import iniciar_servidor, codigo_queue, conexion_queue, mostrar_ventana_qr

    class MiApp(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.ip_escaner, self.puerto_escaner = iniciar_servidor()
            self.after(200, self.revisar_cola_escaner)
            self.after(500, self.revisar_estado_escaner)

        def abrir_qr_escaner(self):
            mostrar_ventana_qr(self, self.ip_escaner, self.puerto_escaner)

        def revisar_cola_escaner(self):
            try:
                while True:
                    codigo = codigo_queue.get_nowait()
                    self.procesar_codigo_escaneado(codigo)
            except queue.Empty:
                pass
            self.after(200, self.revisar_cola_escaner)

        def revisar_estado_escaner(self):
            estado = estado_queue.get() if not estado_queue.empty() else None
            if estado == "conectado":
                self._actualizar_indicador_escaner(True)
            elif estado == "desconectado":
                self._actualizar_indicador_escaner(False)
            self.after(500, self.revisar_estado_escaner)

        def _actualizar_indicador_escaner(self, conectado):
            if conectado:
                self.lbl_estado_escaner.configure(text="📱 Conectado", text_color="#1F8B4C")
            else:
                self.lbl_estado_escaner.configure(text="📱 Desconectado", text_color="#C0392B")
"""

import threading
import socket
import queue
import os
import sys
from flask import Flask, request, send_from_directory

try:
    import qrcode
    _QR_DISPONIBLE = True
except ImportError:
    _QR_DISPONIBLE = False

codigo_queue = queue.Queue()
conexion_queue = queue.Queue()
estado_queue = queue.Queue()

_clientes_conectados = set()
_clientes_lock = threading.Lock()

_app_flask = Flask(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)


@_app_flask.route("/static/<path:filename>")
def _static(filename):
    return send_from_directory(STATIC_DIR, filename)


HTML_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Escáner ExacStock</title>
<style>
  body { font-family: sans-serif; background:#F5EEDE; color:#1B3A57; margin:0; padding:16px; text-align:center; }
  #reader-wrap { position: relative; max-width: 480px; margin: 0 auto; }
  #reader { width: 100%; border-radius: 12px; overflow: hidden; }
  #resultado {
    margin-top: 16px; font-size: 1.6em; min-height: 2em;
    word-break: break-all; font-weight: bold;
  }
  #contador { color: #1B3A57; font-size: 0.9em; opacity: 0.75; }
  #estado-conexion {
    display: inline-block; margin-top: 10px; padding: 6px 14px;
    border-radius: 20px; font-size: 0.9em; font-weight: bold;
  }
  .conectado { background: #DCF7E3; color: #1F8B4C; }
  .desconectado { background: #FDE8E8; color: #C0392B; }
  #btnFlash {
    position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%);
    z-index: 20; display: none; padding: 10px 22px; border-radius: 20px;
    border: none; background: rgba(157,124,232,0.92); color: #fff; font-size: 1em;
  }
  #reconectar-msg { color: #C0392B; margin-top: 8px; font-size: 0.9em; display: none; }
</style>
</head>
<body>
  <h1>Escáner ExacStock</h1>
  <div id="reader-wrap">
    <div id="reader"></div>
    <button id="btnFlash" onclick="toggleFlash()">🔦 Flash</button>
  </div>
  <div id="estado-conexion" class="conectado">✅ Conectado a la PC</div>
  <div id="resultado">Apunta la cámara al código de barras...</div>
  <div id="contador">Escaneados: 0</div>
  <div id="reconectar-msg">⚠ Se perdió la conexión. La página se recargará automáticamente...</div>

  <script src="/static/barcode-detector.min.js"></script>
  <script src="/static/html5-qrcode.min.js"></script>
  <script>
    let total = 0;
    let ultimoCodigo = "";
    let ultimoTiempo = 0;
    let pingInterval = null;

    function onScanSuccess(decodedText) {
      const ahora = Date.now();
      if (decodedText === ultimoCodigo && (ahora - ultimoTiempo) < 1500) return;
      ultimoCodigo = decodedText;
      ultimoTiempo = ahora;

      fetch('/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({codigo: decodedText})
      });

      total++;
      document.getElementById('resultado').innerText = decodedText;
      document.getElementById('resultado').style.fontSize = decodedText.length > 20 ? '1.2em' : '1.6em';
      document.getElementById('contador').innerText = 'Escaneados: ' + total;
      if (navigator.vibrate) navigator.vibrate(80);
    }

    const formatosSoportados = [
      Html5QrcodeSupportedFormats.QR_CODE,
      Html5QrcodeSupportedFormats.EAN_13,
      Html5QrcodeSupportedFormats.EAN_8,
      Html5QrcodeSupportedFormats.UPC_A,
      Html5QrcodeSupportedFormats.UPC_E,
      Html5QrcodeSupportedFormats.UPC_EAN_EXTENSION,
      Html5QrcodeSupportedFormats.CODE_128,
      Html5QrcodeSupportedFormats.CODE_39,
      Html5QrcodeSupportedFormats.CODE_93,
      Html5QrcodeSupportedFormats.CODABAR,
      Html5QrcodeSupportedFormats.ITF,
      Html5QrcodeSupportedFormats.DATA_MATRIX,
    ];

    const scanner = new Html5Qrcode("reader", {
      formatsToSupport: formatosSoportados,
      experimentalFeatures: {
        useBarCodeDetectorIfSupported: true,
      },
      verbose: false,
    });
    let flashPrendido = false;

    function toggleFlash() {
      const capacidades = scanner.getRunningTrackCameraCapabilities();
      const flash = capacidades.torchFeature();
      flashPrendido = !flashPrendido;
      flash.apply(flashPrendido);
      document.getElementById('btnFlash').innerText = flashPrendido
        ? '🔦 Apagar flash' : '🔦 Encender flash';
    }

    function iniciarPing() {
      pingInterval = setInterval(() => {
        fetch('/ping', { method: 'GET' })
          .then(r => {
            if (!r.ok) throw new Error('no ok');
            document.getElementById('estado-conexion').innerText = '✅ Conectado a la PC';
            document.getElementById('estado-conexion').className = 'conectado';
            document.getElementById('reconectar-msg').style.display = 'none';
          })
          .catch(() => {
            document.getElementById('estado-conexion').innerText = '❌ Desconectado';
            document.getElementById('estado-conexion').className = 'desconectado';
            document.getElementById('reconectar-msg').style.display = 'block';
            clearInterval(pingInterval);
            setTimeout(() => location.reload(), 5000);
          });
      }, 3000);
    }

    scanner.start(
      { facingMode: "environment" },
      {
        fps: 15,
        qrbox: { width: 300, height: 170 },
        videoConstraints: {
          facingMode: "environment",
          width: { ideal: 1920 },
          height: { ideal: 1080 },
          advanced: [{ focusMode: "continuous" }],
        },
      },
      onScanSuccess
    ).then(() => {
      try {
        const capacidades = scanner.getRunningTrackCameraCapabilities();
        if (capacidades.torchFeature().isSupported()) {
          document.getElementById('btnFlash').style.display = 'inline-block';
        }
      } catch (e) { }
      iniciarPing();
    }).catch(err => {
      document.getElementById('resultado').innerText = 'Error de cámara: ' + err;
    });
  </script>
</body>
</html>
"""


@_app_flask.route("/")
def _index():
    conexion_queue.put(True)
    with _clientes_lock:
        _clientes_conectados.add(request.remote_addr)
    return HTML_PAGE


@_app_flask.route("/scan", methods=["POST"])
def _scan():
    data = request.get_json(force=True, silent=True) or {}
    codigo = str(data.get("codigo", "")).strip()
    if codigo:
        codigo_queue.put(codigo)
    return {"ok": True}


@_app_flask.route("/ping", methods=["GET"])
def _ping():
    estado_queue.put("conectado")
    return {"ok": True}


def obtener_ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def iniciar_servidor(puerto=5000):
    """
    Arranca el servidor Flask en un hilo en segundo plano con HTTPS
    autofirmado. Es necesario porque los navegadores bloquean la cámara
    en páginas HTTP que no sean 'localhost'.

    Requiere: pip install pyopenssl
    """
    hilo = threading.Thread(
        target=lambda: _app_flask.run(
            host="0.0.0.0", port=puerto, debug=False, use_reloader=False,
            ssl_context="adhoc",
        ),
        daemon=True,
    )
    hilo.start()
    return obtener_ip_local(), puerto


def generar_qr_imagen(url, tamano=300):
    if not _QR_DISPONIBLE:
        raise RuntimeError(
            "Falta instalar qrcode. Ejecuta: pip install qrcode[pil]"
        )
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    imagen = imagen.resize((tamano, tamano))
    return imagen


def mostrar_ventana_qr(parent, ip, puerto):
    import customtkinter as ctk

    url = f"https://{ip}:{puerto}"

    ventana = ctk.CTkToplevel(parent)
    ventana.title("Escanear con el celular")
    ventana.geometry("360x440")
    ventana.resizable(False, False)
    ventana.configure(fg_color="#F5EEDE")

    ventana.transient(parent)

    ctk.CTkLabel(
        ventana, text="Abre la cámara de tu celular\ny apunta a este código",
        font=("", 14), justify="center"
    ).pack(pady=(20, 10))

    if _QR_DISPONIBLE:
        imagen = generar_qr_imagen(url)
        ctk_img = ctk.CTkImage(light_image=imagen, dark_image=imagen, size=(280, 280))
        ctk.CTkLabel(ventana, image=ctk_img, text="").pack(pady=10)
    else:
        ctk.CTkLabel(
            ventana,
            text="(Instala 'qrcode[pil]' para ver el código QR)",
            text_color="orange"
        ).pack(pady=10)

    ctk.CTkLabel(ventana, text="O escribe esta dirección en Chrome:").pack(pady=(10, 0))
    campo_url = ctk.CTkEntry(ventana, width=280, justify="center")
    campo_url.insert(0, url)
    campo_url.configure(state="readonly")
    campo_url.pack(pady=8)

    ventana.update_idletasks()
    x = parent.winfo_x() + (parent.winfo_width() // 2) - (ventana.winfo_width() // 2)
    y = parent.winfo_y() + (parent.winfo_height() // 2) - (ventana.winfo_height() // 2)
    ventana.geometry(f"+{x}+{y}")

    ventana.lift()
    ventana.focus_force()
    ventana.grab_set()
    ventana.attributes("-topmost", True)
    ventana.after(250, lambda: ventana.attributes("-topmost", False))

    while True:
        try:
            conexion_queue.get_nowait()
        except queue.Empty:
            break

    def _revisar_conexion():
        if not ventana.winfo_exists():
            return
        try:
            conexion_queue.get_nowait()
            ventana.grab_release()
            ventana.destroy()
            return
        except queue.Empty:
            pass
        ventana.after(300, _revisar_conexion)

    ventana.after(300, _revisar_conexion)

    return ventana
