"""
Modulo: mobile_scanner.py
--------------------------
Convierte el celular en un lector de codigo de barras inalambrico para
la app "Conteo de Inventario".

Requiere:
    pip install flask
    pip install qrcode[pil]   (opcional, para mostrar el QR de conexion)

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
                self.lbl_estado_escaner.configure(text="Conectado", text_color="#1F8B4C")
            else:
                self.lbl_estado_escaner.configure(text="Desconectado", text_color="#C0392B")
"""

import threading
import socket
import queue
import os
import sys
import time
import secrets
import string
from functools import wraps
from flask import Flask, request, send_from_directory, abort

try:
    import qrcode
    _QR_DISPONIBLE = True
except ImportError:
    _QR_DISPONIBLE = False

import logging
_log_scanner = logging.getLogger("conteo_inventario.scanner")

codigo_queue = queue.Queue()
conexion_queue = queue.Queue()
estado_queue = queue.Queue()

_clientes_conectados = set()
_clientes_lock = threading.Lock()

_app_flask = Flask(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

_pairing_code = None
_rate_limit_timestamps = []
_rate_lock = threading.Lock()
MAX_SCANS_PER_SECOND = 20
MAX_CODIGO_LENGTH = 200


def _generar_pairing_code():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _verificar_pairing():
    if _pairing_code is None:
        return True
    token = request.args.get("token", "") or request.headers.get("X-Pairing-Token", "")
    if not secrets.compare_digest(token, _pairing_code):
        abort(403)


def _rate_limit():
    now = time.time()
    with _rate_lock:
        _rate_limit_timestamps[:] = [t for t in _rate_limit_timestamps if now - t < 1.0]
        if len(_rate_limit_timestamps) >= MAX_SCANS_PER_SECOND:
            abort(429)
        _rate_limit_timestamps.append(now)


def _agregar_headers_seguridad(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_app_flask.after_request(_agregar_headers_seguridad)


@_app_flask.route("/static/<path:filename>")
def _static(filename):
    return send_from_directory(STATIC_DIR, filename)


HTML_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Escanner ExacStock</title>
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
  #pairing-screen { padding: 40px 20px; }
  #pairing-screen h2 { margin-bottom: 20px; }
  #pairing-input {
    font-size: 1.8em; text-align: center; letter-spacing: 8px;
    width: 200px; padding: 12px; border: 2px solid #1B3A57; border-radius: 10px;
    text-transform: uppercase; font-weight: bold;
  }
  #pairing-btn {
    display: block; margin: 20px auto 0; padding: 12px 40px;
    background: #1B3A57; color: #fff; border: none; border-radius: 10px;
    font-size: 1.1em; cursor: pointer;
  }
  #pairing-error {
    color: #fff; margin-top: 15px; display: none; font-size: 1em; font-weight: bold;
    background: #C0392B; padding: 12px 20px; border-radius: 10px;
    max-width: 280px; margin-left: auto; margin-right: auto;
  }
  #pairing-input.error { border-color: #C0392B; animation: shake 0.4s; }
  @keyframes shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-8px); }
    75% { transform: translateX(8px); }
  }
</style>
</head>
<body>
  <div id="pairing-screen">
    <h1>ExacStock</h1>
    <h2>Ingresa el codigo de conexion</h2>
    <p>Mira el codigo en la pantalla de la PC</p>
    <input id="pairing-input" type="text" maxlength="6" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="------">
    <button id="pairing-btn" onclick="verificarCodigo()">Conectar</button>
    <div id="pairing-error">Codigo incorrecto. Intenta de nuevo.</div>
  </div>

  <div id="scanner-screen" style="display:none;">
    <h1>Escanner ExacStock</h1>
    <div id="reader-wrap">
      <div id="reader"></div>
      <button id="btnFlash" style="display:none">Flash</button>
    </div>
    <div id="estado-conexion" class="conectado">Conectado a la PC</div>
    <div id="resultado">Apunta la camara al codigo de barras...</div>
    <div id="contador">Escaneados: 0</div>
    <div id="reconectar-msg">Se perdio la conexion. La pagina se recargara automaticamente...</div>
  </div>

  <script src="/static/barcode-detector.min.js"></script>
  <script src="/static/html5-qrcode.min.js"></script>
  <script>
    let total = 0;
    let ultimoCodigo = "";
    let ultimoTiempo = 0;
    let pingInterval = null;
    let pairingToken = "";
    let scannerActivo = null;

    function mostrarErrorCodigo(msg) {
      const inp = document.getElementById('pairing-input');
      const err = document.getElementById('pairing-error');
      inp.classList.remove('error');
      void inp.offsetWidth;
      inp.classList.add('error');
      err.innerText = msg;
      err.style.display = 'block';
      inp.value = '';
      inp.focus();
    }

    function verificarCodigo() {
      const code = document.getElementById('pairing-input').value.trim().toUpperCase();
      if (code.length !== 6) return;
      document.getElementById('pairing-btn').disabled = true;
      fetch('/verify?token=' + encodeURIComponent(code))
        .then(r => r.json())
        .then(data => {
          if (data.ok) {
            pairingToken = code;
            document.getElementById('pairing-screen').style.display = 'none';
            document.getElementById('scanner-screen').style.display = 'block';
            iniciarScanner();
          } else {
            mostrarErrorCodigo('Codigo incorrecto. Intenta de nuevo.');
          }
          document.getElementById('pairing-btn').disabled = false;
        })
        .catch(() => {
          mostrarErrorCodigo('Error de conexion. Verifica que estes en la misma red.');
          document.getElementById('pairing-btn').disabled = false;
        });
    }

    document.getElementById('pairing-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') verificarCodigo();
    });

    function iniciarScanner() {
      if (scannerActivo) {
        try { scannerActivo.stop(); } catch(e) {}
        scannerActivo = null;
      }
      document.getElementById('reader').innerHTML = '';
      if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }

      const res = document.getElementById('resultado');
      res.innerText = 'Iniciando camara...';

      function onScanSuccess(decodedText) {
        const ahora = Date.now();
        if (decodedText === ultimoCodigo && (ahora - ultimoTiempo) < 1500) return;
        ultimoCodigo = decodedText;
        ultimoTiempo = ahora;

        const token = pairingToken || '';
        fetch('/scan?token=' + encodeURIComponent(token), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({codigo: decodedText})
        }).then(r => {
          const cnt = document.getElementById('contador');
          if (r.ok) {
            cnt.style.color = '#1F8B4C';
            cnt.innerText = 'Enviado a la PC';
          } else {
            cnt.style.color = '#C0392B';
            cnt.innerText = 'Error ' + r.status + ' - Revisa el codigo';
          }
          setTimeout(() => {
            cnt.style.color = '';
            cnt.innerText = 'Escaneados: ' + total;
          }, 1500);
        }).catch(err => {
          const cnt = document.getElementById('contador');
          cnt.style.color = '#C0392B';
          cnt.innerText = 'Sin conexion: ' + err;
          setTimeout(() => {
            cnt.style.color = '';
            cnt.innerText = 'Escaneados: ' + total;
          }, 2000);
        });

        total++;
        res.innerText = decodedText;
        res.style.fontSize = decodedText.length > 20 ? '1.2em' : '1.6em';
        document.getElementById('contador').innerText = 'Escaneados: ' + total;
        if (navigator.vibrate) navigator.vibrate(80);
        try {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.frequency.value = 1800;
          osc.type = 'square';
          gain.gain.value = 0.5;
          osc.start();
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
          osc.stop(ctx.currentTime + 0.15);
        } catch(e) {}
      }

      try {
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
          experimentalFeatures: { useBarCodeDetectorIfSupported: true },
          verbose: true,
        });
        scannerActivo = scanner;
        let flashPrendido = false;

        function toggleFlash() {
          const capacidades = scanner.getRunningTrackCameraCapabilities();
          const flash = capacidades.torchFeature();
          flashPrendido = !flashPrendido;
          flash.apply(flashPrendido);
          document.getElementById('btnFlash').innerText = flashPrendido
            ? 'Apagar flash' : 'Encender flash';
        }

        function iniciarPing() {
          pingInterval = setInterval(() => {
            fetch('/ping?token=' + encodeURIComponent(pairingToken), { method: 'GET' })
              .then(r => {
                if (!r.ok) throw new Error('no ok');
                document.getElementById('estado-conexion').innerText = 'Conectado a la PC';
                document.getElementById('estado-conexion').className = 'conectado';
                document.getElementById('reconectar-msg').style.display = 'none';
              })
              .catch(() => {
                document.getElementById('estado-conexion').innerText = 'Desconectado';
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
            aspectRatio: 1.0,
            disableFlip: false,
            videoConstraints: {
              facingMode: "environment",
              width: { ideal: 1280 },
              height: { ideal: 720 },
              advanced: [{ focusMode: "continuous" }],
            },
          },
          onScanSuccess
        ).then(() => {
          try {
            const capacidades = scanner.getRunningTrackCameraCapabilities();
            if (capacidades.torchFeature().isSupported()) {
              const btnFlash = document.getElementById('btnFlash');
              btnFlash.style.display = 'inline-block';
              btnFlash.onclick = toggleFlash;
            }
          } catch (e) { }
          res.innerText = 'Camara activa. Apunta al codigo de barras...';
          iniciarPing();
        }).catch(err => {
          console.error('Error start:', err);
          res.innerText = 'Error de camara: ' + err;
          res.style.color = '#C0392B';
        });
      } catch(e) {
        console.error('Error init:', e);
        res.innerText = 'Error al iniciar: ' + e;
        res.style.color = '#C0392B';
      }
    }
  </script>
</body>
</html>
"""


@_app_flask.route("/verify")
def _verify():
    token = request.args.get("token", "")
    if _pairing_code and secrets.compare_digest(token, _pairing_code):
        conexion_queue.put(True)
        return {"ok": True}
    return {"ok": False}, 403


@_app_flask.route("/")
def _index():
    with _clientes_lock:
        if len(_clientes_conectados) < 50:
            _clientes_conectados.add(request.remote_addr)
    return HTML_PAGE


@_app_flask.route("/scan", methods=["POST"])
def _scan():
    _verificar_pairing()
    _rate_limit()
    data = request.get_json(force=True, silent=True) or {}
    codigo = str(data.get("codigo", "")).strip()
    if len(codigo) > MAX_CODIGO_LENGTH:
        codigo = codigo[:MAX_CODIGO_LENGTH]
    print(f"[SCANNER] raw={data.get('codigo')!r} stripped={codigo!r} printable={all(c.isprintable() for c in codigo) if codigo else False} qsize={codigo_queue.qsize()}", flush=True)
    if codigo and all(c.isprintable() for c in codigo):
        codigo_queue.put(codigo)
        print(f"[SCANNER] PUT en cola OK, qsize={codigo_queue.qsize()}", flush=True)
    else:
        print(f"[SCANNER] RECHAZADO: codigo={codigo!r}", flush=True)
    return {"ok": True}


@_app_flask.route("/ping", methods=["GET"])
def _ping():
    _verificar_pairing()
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
    autofirmado y codigo de emparejamiento de 6 caracteres.

    Requiere: pip install pyopenssl
    """
    global _pairing_code
    _pairing_code = _generar_pairing_code()

    hilo = threading.Thread(
        target=lambda: _app_flask.run(
            host="0.0.0.0", port=puerto, debug=False, use_reloader=False,
            ssl_context="adhoc",
        ),
        daemon=True,
    )
    hilo.start()
    return obtener_ip_local(), puerto


def obtener_pairing_code():
    return _pairing_code


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

    pairing = obtener_pairing_code()
    url = f"https://{ip}:{puerto}"

    ventana = ctk.CTkToplevel(parent)
    ventana.title("Escanear con el celular")
    ventana.geometry("360x520")
    ventana.resizable(False, False)
    ventana.configure(fg_color="#F5EEDE")

    ventana.transient(parent)

    ctk.CTkLabel(
        ventana, text="Abre la camara de tu celular\ny apunta a este codigo",
        font=("", 14), justify="center"
    ).pack(pady=(15, 5))

    if _QR_DISPONIBLE:
        imagen = generar_qr_imagen(url)
        ctk_img = ctk.CTkImage(light_image=imagen, dark_image=imagen, size=(250, 250))
        ctk.CTkLabel(ventana, image=ctk_img, text="").pack(pady=5)
    else:
        ctk.CTkLabel(
            ventana,
            text="(Instala 'qrcode[pil]' para ver el codigo QR)",
            text_color="orange"
        ).pack(pady=10)

    ctk.CTkLabel(ventana, text="Codigo de conexion:", font=("", 12, "bold")).pack(pady=(8, 2))
    lbl_code = ctk.CTkLabel(
        ventana, text=pairing,
        font=ctk.CTkFont(size=24, weight="bold"),
        text_color="#1A3A5C"
    )
    lbl_code.pack(pady=2)

    ctk.CTkLabel(ventana, text="(Ingresa este codigo en el celular)", font=("", 10), text_color="#888").pack(pady=(0, 5))

    ctk.CTkLabel(ventana, text="O escribe esta direccion en Chrome:").pack(pady=(5, 0))
    campo_url = ctk.CTkEntry(ventana, width=280, justify="center")
    campo_url.insert(0, url)
    campo_url.configure(state="readonly")
    campo_url.pack(pady=5)

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
