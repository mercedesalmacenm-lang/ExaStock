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
    # Excluye caracteres que se confunden al escribirlos en el celular: 0/O, 1/I/L.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
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
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob: https://cdn.jsdelivr.net; "
        "worker-src 'self' blob: https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' https://cdn.jsdelivr.net https://storage.googleapis.com; "
        "frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    # Evitar que el navegador del celular guarde versiones viejas de la
    # pagina del escaner (causa de bugs "no me actualiza").
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


_app_flask.after_request(_agregar_headers_seguridad)


@_app_flask.route("/static/<path:filename>")
def _static(filename):
    return send_from_directory(STATIC_DIR, filename)


HTML_PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ExaStock Scanner</title>
<style>
  :root {
    --navy: #1A3A5C;
    --navy-dark: #0D1B2A;
    --navy-hover: #2A4A6E;
    --gold: #C9A84C;
    --gold-dark: #B89430;
    --cream: #F5F0E8;
    --cream-light: #FBF7EE;
    --text: #1B3A57;
    --muted: #8A9BB0;
    --ok: #1F8B4C;
    --ok-bg: #DCF7E3;
    --err: #C0392B;
    --err-bg: #FDE8E8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: linear-gradient(180deg, #EDE6D8 0%, var(--cream) 40%);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  header {
    width: 100%;
    background: linear-gradient(135deg, var(--navy-dark), var(--navy));
    padding: 18px 20px;
    text-align: center;
    box-shadow: 0 3px 14px rgba(13,27,42,0.35);
  }
  header h1 { font-size: 1.7em; letter-spacing: 1px; }
  header h1 .exa { color: var(--gold); font-weight: 800; }
  header h1 .stock { color: #fff; font-weight: 300; }
  header .tagline { color: #B9C9DB; font-size: 0.8em; margin-top: 2px; }
  main { flex: 1; width: 100%; max-width: 520px; padding: 16px; }
  .card {
    background: var(--cream-light);
    border-radius: 18px;
    padding: 20px;
    box-shadow: 0 6px 24px rgba(13,27,42,0.14);
    border: 1px solid #E7DFCF;
  }
  .section-title {
    font-size: 1.05em; font-weight: 700; color: var(--navy);
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  }
  .section-title .bar {
    width: 4px; height: 18px; background: var(--gold); border-radius: 2px;
  }

  /* ── Pantalla de emparejamiento ── */
  #pairing-screen { padding: 8px 6px; text-align: center; }
  #pairing-screen .logo-circle {
    width: 76px; height: 76px; margin: 6px auto 14px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--navy-dark), var(--navy));
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px rgba(13,27,42,0.3);
  }
  #pairing-screen .logo-circle span {
    color: var(--gold); font-size: 2em; font-weight: 800;
  }
  #pairing-screen h2 { font-size: 1.25em; color: var(--navy); margin-bottom: 6px; }
  #pairing-screen p { color: var(--muted); font-size: 0.92em; margin-bottom: 22px; }
  #pairing-input {
    font-size: 2.1em; text-align: center; letter-spacing: 12px;
    width: 210px; padding: 14px 8px;
    border: 2px solid var(--navy); border-radius: 14px;
    text-transform: uppercase; font-weight: 800;
    background: #fff; color: var(--navy);
    outline: none;
  }
  #pairing-input:focus { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(201,168,76,0.25); }
  #pairing-btn {
    display: block; margin: 22px auto 0; padding: 14px 48px;
    background: linear-gradient(135deg, var(--navy), var(--navy-hover));
    color: #fff; border: none; border-radius: 12px;
    font-size: 1.1em; font-weight: 700; cursor: pointer;
    box-shadow: 0 4px 14px rgba(26,58,92,0.35);
    transition: transform 0.1s, box-shadow 0.2s;
  }
  #pairing-btn:active { transform: scale(0.97); }
  #pairing-btn:disabled { opacity: 0.6; }
  #pairing-error {
    color: #fff; margin-top: 16px; display: none; font-size: 0.95em; font-weight: 700;
    background: var(--err); padding: 12px 18px; border-radius: 12px;
    max-width: 280px; margin-left: auto; margin-right: auto;
    box-shadow: 0 4px 12px rgba(192,57,43,0.3);
  }
  #pairing-input.error { border-color: var(--err); animation: shake 0.4s; }
  @keyframes shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-8px); }
    75% { transform: translateX(8px); }
  }

  /* ── Pantalla de escáner ── */
  #scanner-screen { display: none; }
  #reader-wrap {
    position: relative; border-radius: 16px; overflow: hidden;
    box-shadow: 0 6px 24px rgba(13,27,42,0.25);
    border: 3px solid var(--navy);
  }
  #reader { width: 100%; min-height: 260px; background: #000; }
  #estado-conexion {
    display: inline-block; margin: 14px auto 4px; padding: 6px 16px;
    border-radius: 20px; font-size: 0.9em; font-weight: 700;
  }
  .conectado { background: var(--ok-bg); color: var(--ok); }
  .desconectado { background: var(--err-bg); color: var(--err); }
  #btnFlash {
    position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%);
    z-index: 20; display: none; padding: 10px 24px; border-radius: 22px;
    border: none; background: rgba(26,58,92,0.92); color: #fff; font-size: 0.95em;
    font-weight: 700; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  }
  #reconectar-msg { color: var(--err); margin-top: 8px; font-size: 0.9em; display: none; font-weight: 600; }
  #resultado {
    margin-top: 12px; font-size: 1.5em; min-height: 1.8em;
    word-break: break-all; font-weight: 800; color: var(--navy);
    background: #F3EDDF; border-radius: 12px; padding: 10px 14px;
  }
  #resultado.ok { background: var(--ok-bg); color: var(--ok); }
  #resultado.err { background: var(--err-bg); color: var(--err); }
  #contador {
    color: var(--muted); font-size: 0.9em; font-weight: 600; margin-top: 4px;
  }
  #motor-info {
    color: #8a8578; font-size: 0.78em; margin-top: 6px; font-weight: 600;
  }
  #debug-log {
    display: none;
    color: #b03a2e; font-size: 0.75em; margin-top: 6px; text-align: left;
    white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow: auto;
    font-family: monospace;
  }

  /* ── Entrada manual ── */
  .manual-card { margin-top: 16px; }
  .manual-row { display: flex; gap: 8px; }
  #manual-input {
    flex: 1; padding: 13px 14px; font-size: 1.05em; font-weight: 700;
    border: 2px solid #D8CFBA; border-radius: 12px; color: var(--navy);
    background: #fff; outline: none; text-transform: uppercase;
    min-width: 0;
  }
  #manual-input:focus { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(201,168,76,0.25); }
  #manual-cant {
    width: 72px; padding: 13px 8px; font-size: 1.05em; font-weight: 700;
    border: 2px solid #D8CFBA; border-radius: 12px; color: var(--navy);
    background: #fff; outline: none; text-align: center;
  }
  #manual-cant:focus { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(201,168,76,0.25); }
  #btn-manual {
    padding: 0 20px; border: none; border-radius: 12px;
    background: linear-gradient(135deg, var(--gold), var(--gold-dark));
    color: var(--navy-dark); font-size: 1em; font-weight: 800; cursor: pointer;
    box-shadow: 0 3px 10px rgba(184,148,48,0.35);
    transition: transform 0.1s;
  }
  #btn-manual:active { transform: scale(0.97); }
  .manual-hint { color: var(--muted); font-size: 0.82em; margin-top: 8px; }
</style>
</head>
<body>
  <header>
    <h1><span class="exa">Exa</span><span class="stock">Stock</span></h1>
    <div class="tagline">Conteo de inventario · Escáner</div>
  </header>

  <main>
    <div id="debug-log"></div>

    <div id="pairing-screen" class="card">
      <div class="logo-circle"><span>E</span></div>
      <h2>Conecta tu celular</h2>
      <p>Ingresa el código que aparece en la pantalla de la PC</p>
      <input id="pairing-input" type="text" maxlength="6" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="------">
      <button id="pairing-btn" onclick="verificarCodigo()">Conectar</button>
      <div id="pairing-error">Codigo incorrecto. Intenta de nuevo.</div>
    </div>

    <div id="scanner-screen">
      <div class="card" style="padding:14px;">
        <div id="reader-wrap">
          <div id="reader"></div>
          <button id="btnFlash" style="display:none">⚡ Flash</button>
        </div>
        <div style="text-align:center;">
          <div id="estado-conexion" class="conectado">Conectado a la PC</div>
          <div id="resultado">Apunta la cámara al código de barras...</div>
          <div id="contador">Escaneados: 0</div>
          <div id="motor-info"></div>
          <div id="reconectar-msg">Se perdió la conexión. La página se recargará automáticamente...</div>
        </div>
      </div>

      <div class="card manual-card">
        <div class="section-title"><span class="bar"></span>Entrada manual</div>
        <div class="manual-row">
          <input id="manual-input" type="text" maxlength="60" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="Código...">
          <input id="manual-cant" type="number" min="1" step="any" value="1" inputmode="decimal" placeholder="Cant">
          <button id="btn-manual" onclick="enviarManual()">Enviar</button>
        </div>
        <div class="manual-hint">Para códigos difíciles de leer o sin etiqueta, escribe el código y la cantidad.</div>
      </div>
    </div>
  </main>

  <script src="/static/html5-qrcode.min.js"></script>
  <script src="/static/zxing/zxing_reader.js"></script>
  <script type="module">
    const MP_CDN = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.22';
    (async () => {
      try {
        const mod = await import(MP_CDN + '/vision_bundle.mjs');
        window.__exaMediaPipe = {
          BarcodeScanner: mod.BarcodeScanner,
          FilesetResolver: mod.FilesetResolver,
          CDN: MP_CDN,
        };
      } catch (e) {
        console.error('MediaPipe no disponible:', e);
        window.__exaMediaPipe = null;
        window.__exaMediaPipeError = String(e && e.message ? e.message : e);
      }
    })();
  </script>
  <script>
    let total = 0;
    let ultimoCodigo = "";
    let ultimoTiempo = 0;
    let pingInterval = null;
    let pairingToken = "";
    let scannerActivo = null;

    logMsg('Pagina cargada · v3.0 · ' + new Date().toLocaleTimeString());

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

    function ocultarErrorCodigo() {
      const inp = document.getElementById('pairing-input');
      const err = document.getElementById('pairing-error');
      inp.classList.remove('error');
      err.style.display = 'none';
    }

    function mostrarPantallaScanner() {
      logMsg('mostrarPantallaScanner()');
      document.getElementById('pairing-screen').style.display = 'none';
      document.getElementById('scanner-screen').style.display = 'block';
      iniciarScanner();
    }

    function verificarCodigo() {
      ocultarErrorCodigo();
      const code = document.getElementById('pairing-input').value.trim().toUpperCase();
      logMsg('Conectar presionado, codigo: "' + code + '" (' + code.length + ' chars)');
      if (code.length !== 6) {
        logMsg('Codigo con largo invalido, ignorando');
        return;
      }
      const btn = document.getElementById('pairing-btn');
      btn.disabled = true;
      btn.innerText = 'Verificando...';
      const limpiar = () => {
        btn.disabled = false;
        btn.innerText = 'Conectar';
      };
      logMsg('Enviando /verify...');
      fetch('/verify?token=' + encodeURIComponent(code))
        .then(r => {
          logMsg('Respuesta /verify status: ' + r.status);
          return r.json();
        })
        .then(data => {
          logMsg('Respuesta /verify data: ' + JSON.stringify(data));
          if (data.ok) {
            pairingToken = code;
            logMsg('Codigo valido, abriendo escaner...');
            mostrarPantallaScanner();
          } else {
            mostrarErrorCodigo('Codigo incorrecto. Intenta de nuevo.');
          }
        })
        .catch(err => {
          logError('Error /verify: ' + (err && err.message ? err.message : err));
          mostrarErrorCodigo('Sin conexión a la PC. Verifica que estés en la misma red Wi-Fi y que ExaStock siga abierto. Si la PC se reinició, recarga esta página.');
        })
        .finally(limpiar);
    }

    document.getElementById('pairing-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') verificarCodigo();
    });

    function mostrarFeedback(texto, tipo) {
      const res = document.getElementById('resultado');
      res.innerText = texto;
      res.style.fontSize = texto.length > 20 ? '1.1em' : '1.5em';
      res.className = tipo || '';
    }

    function enviarCodigo(codigo, cantidad) {
      const token = pairingToken || '';
      const payload = {codigo: codigo};
      if (cantidad && cantidad !== 1) payload.cantidad = cantidad;
      fetch('/scan?token=' + encodeURIComponent(token), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      }).then(r => {
        const cnt = document.getElementById('contador');
        if (r.ok) {
          total++;
          mostrarFeedback(codigo, 'ok');
          cnt.innerText = 'Enviado a la PC · Escaneados: ' + total;
          beepOk();
        } else {
          mostrarFeedback(codigo + ' · Error ' + r.status, 'err');
          cnt.style.color = '#C0392B';
          cnt.innerText = 'Error ' + r.status + ' - Revisa el codigo';
          setTimeout(() => {
            cnt.style.color = '';
            cnt.innerText = 'Escaneados: ' + total;
          }, 1500);
        }
      }).catch(err => {
        mostrarFeedback('Sin conexión: ' + err, 'err');
      });
    }

    function enviarManual() {
      const inp = document.getElementById('manual-input');
      const codigo = inp.value.trim();
      if (!codigo) { inp.focus(); return; }
      const cantEl = document.getElementById('manual-cant');
      let cantidad = 1;
      const cantTexto = cantEl.value.trim().replace(',', '.');
      if (cantTexto !== '') {
        const parsed = parseFloat(cantTexto);
        if (parsed > 0) cantidad = parsed;
      }
      enviarCodigo(codigo, cantidad);
      inp.value = '';
      cantEl.value = '1';
      inp.focus();
      if (navigator.vibrate) navigator.vibrate(50);
    }

    function beepOk() {
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

    function mostrarMotor(nombre) {
      const el = document.getElementById('motor-info');
      if (el) el.innerText = 'Motor: ' + nombre + ' · v3.0';
    }

    function logMsg(msg) {
      console.log('[scanner]', msg);
      const el = document.getElementById('debug-log');
      if (el) {
        el.innerText = (el.innerText ? el.innerText + '\n' : '') + msg;
        el.scrollTop = el.scrollHeight;
      }
    }
    function logError(msg) {
      console.error('[scanner]', msg);
      logMsg('ERROR: ' + msg);
      const el = document.getElementById('debug-log');
      if (el) el.style.display = 'block';
    }
    window.addEventListener('error', function(e) {
      logError('Error JS: ' + (e && e.message ? e.message : e));
    });
    window.addEventListener('unhandledrejection', function(e) {
      const r = e && e.reason;
      logError('Promesa rechazada: ' + (r && r.message ? r.message : r));
    });

    async function iniciarScanner() {
      if (scannerActivo) {
        try { scannerActivo.stop(); } catch(e) {}
        scannerActivo = null;
      }
      document.getElementById('reader').innerHTML = '';
      if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }

      const res = document.getElementById('resultado');
      res.innerText = 'Iniciando camara...';
      res.className = '';

      function onScanSuccess(decodedText) {
        const ahora = Date.now();
        if (decodedText === ultimoCodigo && (ahora - ultimoTiempo) < 3000) return;
        ultimoCodigo = decodedText;
        ultimoTiempo = ahora;
        enviarCodigo(decodedText);
        if (navigator.vibrate) navigator.vibrate(80);
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

      // ── Motor offline: html5-qrcode con decodificador ZXing propio ──
      function iniciarConHtml5Qrcode() {
        const formatosSoportados = [
          Html5QrcodeSupportedFormats.QR_CODE,
          Html5QrcodeSupportedFormats.CODE_128,
        ];

        const scanner = new Html5Qrcode("reader", {
          formatsToSupport: formatosSoportados,
          experimentalFeatures: { useBarCodeDetectorIfSupported: false },
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
            ? '⚡ Apagar flash' : '⚡ Encender flash';
        }

        scanner.start(
          { facingMode: "environment" },
          {
            fps: 15,
            disableFlip: false,
            qrbox: function(vw, vh) {
              return { width: Math.floor(vw * 0.94), height: Math.min(80, Math.floor(vh * 0.22)) };
            },
            videoConstraints: {
              facingMode: "environment",
              width: { ideal: 640 },
              height: { ideal: 480 },
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
          res.className = '';
          mostrarMotor('ZXing (lento, respaldo)');
          logMsg('ZXing iniciado OK');
          iniciarPing();
        }).catch(err => {
          console.error('Error start:', err);
          res.innerText = 'Error de camara: ' + err;
          res.className = 'err';
          logError('ZXing error: ' + (err && err.message ? err.message : err));
        });
      }

      // ── Motor offline: ZXing compilado a WASM (rapido, sin internet) ──
      async function iniciarConZxingWasm() {
        const readerEl = document.getElementById('reader');
        readerEl.innerHTML = '';
        const video = document.createElement('video');
        video.autoplay = true;
        video.muted = true;
        video.playsInline = true;
        video.setAttribute('playsinline', '');
        video.setAttribute('muted', '');
        video.style.width = '100%';
        video.style.display = 'block';
        video.style.objectFit = 'cover';
        readerEl.appendChild(video);

        const btnFlash = document.getElementById('btnFlash');
        btnFlash.style.display = 'none';

        if (typeof ZXingWASM === 'undefined') {
          throw new Error('ZXingWASM no cargo');
        }
        try {
          ZXingWASM.setZXingModuleOverrides({
            locateFile: (p) => '/static/zxing/' + p,
          });
          await ZXingWASM.getZXingModule();
        } catch (e) {
          throw new Error('init WASM: ' + (e && e.message ? e.message : e));
        }

        res.innerText = 'Cargando camara...';
        res.className = '';

        const stream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: 'environment',
            width: { ideal: 1920 },
            height: { ideal: 1080 },
          },
          audio: false,
        });
        video.srcObject = stream;
        scannerActivo = {
          stop: () => {
            stream.getTracks().forEach(t => t.stop());
            readerEl.innerHTML = '';
            if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
          }
        };
        video.onloadedmetadata = () => video.play().catch(() => {});
        video.play().catch(() => {});

        const track = stream.getVideoTracks()[0];
        if (track && track.getCapabilities && track.getCapabilities().torch) {
          let flashPrendido = false;
          btnFlash.style.display = 'inline-block';
          btnFlash.onclick = () => {
            flashPrendido = !flashPrendido;
            track.applyConstraints({ advanced: [{ torch: flashPrendido }] }).catch(() => {});
            btnFlash.innerText = flashPrendido ? '⚡ Apagar flash' : '⚡ Encender flash';
          };
        }

        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        const MAX_W = 960;
        let zxTrabajando = false;
        let zxContador = 0;

        function frame() {
          if (!scannerActivo || video.readyState < 2 || video.videoWidth === 0) {
            if (scannerActivo) requestAnimationFrame(frame);
            return;
          }
          zxContador++;
          if (zxContador % 3 === 0 && !zxTrabajando) {
            zxTrabajando = true;
            try {
              const escala = Math.min(1, MAX_W / video.videoWidth);
              canvas.width = Math.max(1, Math.round(video.videoWidth * escala));
              canvas.height = Math.max(1, Math.round(video.videoHeight * escala));
              ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
              const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
              ZXingWASM.readBarcodesFromImageData({
                data: new Uint8Array(imageData.data),
                width: canvas.width,
                height: canvas.height,
              }, {
                formats: ['Code128'],
                tryHarder: false,
                tryInvert: false,
                minLineCount: 3,
              }).then(results => {
                if (results) {
                  for (const r of results) {
                    if (r && r.text && r.format === 'Code128') onScanSuccess(r.text);
                  }
                }
              }).catch(() => {}).finally(() => { zxTrabajando = false; });
            } catch (e) {
              zxTrabajando = false;
            }
          }
          if (scannerActivo) requestAnimationFrame(frame);
        }

        res.innerText = 'Camara activa. Apunta al codigo de barras...';
        res.className = '';
        mostrarMotor('ZXing (WASM rapido)');
        logMsg('ZXing WASM iniciado OK');
        iniciarPing();
        requestAnimationFrame(frame);
      }

      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        logMsg('Sin internet detectado. Saltando MediaPipe, usando ZXing WASM offline.');
        try {
          await iniciarConZxingWasm();
        } catch (e) {
          logError('ZXing WASM fallo, respaldo html5-qrcode: ' + (e && e.message ? e.message : e));
          iniciarConHtml5Qrcode();
        }
        return;
      }

      const mp = await esperarMediaPipe(3000);
      if (mp) {
        logMsg('MediaPipe disponible, iniciando...');
        iniciarConMediaPipe(mp);
        return;
      }
      logMsg('MediaPipe no disponible: ' + (window.__exaMediaPipeError || 'sin internet o bloqueado') + '. Usando ZXing WASM offline.');
      try {
        await iniciarConZxingWasm();
      } catch (e) {
        logError('ZXing WASM fallo, respaldo html5-qrcode: ' + (e && e.message ? e.message : e));
        iniciarConHtml5Qrcode();
      }
    }

    // Espera hasta que el modulo de MediaPipe (Google) termine de cargar.
    // Devuelve null si no esta disponible (sin internet o fallo).
    function esperarMediaPipe(ms) {
      return new Promise(resolve => {
        if (window.__exaMediaPipe) { resolve(window.__exaMediaPipe); return; }
        if (window.__exaMediaPipe === null) { resolve(null); return; }
        const inicio = Date.now();
        const iv = setInterval(() => {
          if (window.__exaMediaPipe) { clearInterval(iv); resolve(window.__exaMediaPipe); return; }
          if (window.__exaMediaPipe === null || Date.now() - inicio > ms) {
            clearInterval(iv);
            resolve(window.__exaMediaPipe || null);
          }
        }, 100);
      });
    }

    // ── Motor principal: MediaPipe BarcodeScanner de Google (WASM) ──
    function iniciarConMediaPipe(mp) {
      const reader = document.getElementById('reader');
      reader.innerHTML = '';
      const video = document.createElement('video');
      video.autoplay = true;
      video.muted = true;
      video.playsInline = true;
      video.setAttribute('playsinline', '');
      video.setAttribute('muted', '');
      video.style.width = '100%';
      video.style.display = 'block';
      video.style.objectFit = 'cover';
      reader.appendChild(video);

      const btnFlash = document.getElementById('btnFlash');
      btnFlash.style.display = 'none';
      let flashPrendido = false;

      res.innerText = 'Cargando lector de Google (la primera vez tarda unos segundos)...';
      res.className = '';

      let barcodeScanner = null;
      let mpUltimoTiempo = -1;

      (async () => {
        try {
          const vision = await mp.FilesetResolver.forVisionTasks(mp.CDN + '/wasm');
          barcodeScanner = await mp.BarcodeScanner.createFromOptions(vision, {
            baseOptions: {
              modelAssetPath: 'https://storage.googleapis.com/mediapipe-models/barcode_scanner/barcode_scanner/float16/latest/barcode_scanner.tflite',
            },
            runningMode: 'VIDEO',
          });
        } catch (e) {
          console.error('Error MediaPipe:', e);
          res.innerText = 'Error cargando el lector. Usando respaldo...';
          res.className = 'err';
          logError('MediaPipe fallo: ' + (e && e.message ? e.message : e));
          iniciarConHtml5Qrcode();
          return;
        }

        navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: 'environment',
            width: { ideal: 1920 },
            height: { ideal: 1080 },
          },
          audio: false,
        }).then(stream => {
          video.srcObject = stream;
          scannerActivo = {
            stop: () => {
              stream.getTracks().forEach(t => t.stop());
              reader.innerHTML = '';
              if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
            }
          };
          video.onloadedmetadata = () => video.play().catch(() => {});
          video.play().catch(() => {});

          const track = stream.getVideoTracks()[0];
          if (track && track.getCapabilities && track.getCapabilities().torch) {
            btnFlash.style.display = 'inline-block';
            btnFlash.onclick = () => {
              flashPrendido = !flashPrendido;
              track.applyConstraints({ advanced: [{ torch: flashPrendido }] }).catch(() => {});
              btnFlash.innerText = flashPrendido ? '⚡ Apagar flash' : '⚡ Encender flash';
            };
          }

          function loop() {
            if (scannerActivo && barcodeScanner && video.readyState >= 2 && video.videoWidth > 0) {
              if (video.currentTime !== mpUltimoTiempo) {
                mpUltimoTiempo = video.currentTime;
                try {
                  const resultado = barcodeScanner.detectForVideo(video, performance.now());
                  if (resultado && resultado.barcodes) {
                    for (const b of resultado.barcodes) {
                      if (b && b.rawValue) onScanSuccess(b.rawValue);
                    }
                  }
                } catch (e) {}
              }
            }
            if (scannerActivo) requestAnimationFrame(loop);
          }

          res.innerText = 'Camara activa. Apunta al codigo de barras...';
          res.className = '';
          mostrarMotor('MediaPipe (Google)');
          iniciarPing();
          requestAnimationFrame(loop);
        }).catch(err => {
          console.error('Error camara:', err);
          res.innerText = 'Error de camara o permiso denegado: ' + err;
          res.className = 'err';
        });
      })();
    }

    document.getElementById('manual-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') enviarManual();
    });
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

    cantidad = 1
    try:
        c = data.get("cantidad", 1)
        if isinstance(c, bool):
            raise ValueError
        cantidad = float(c)
        if cantidad <= 0 or cantidad > 9999999:
            cantidad = 1
    except (TypeError, ValueError):
        cantidad = 1

    print(f"[SCANNER] raw={data.get('codigo')!r} stripped={codigo!r} cantidad={cantidad} printable={all(c.isprintable() for c in codigo) if codigo else False} qsize={codigo_queue.qsize()}", flush=True)
    if codigo and all(c.isprintable() for c in codigo):
        codigo_queue.put((codigo, cantidad))
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

    Si el puerto ya esta en uso (por ejemplo, otra copia de ExaStock
    todavia abierta), lanza un RuntimeError claro en lugar de fallar
    en silencio y dejar un codigo de emparejamiento inutil.

    Requiere: pip install pyopenssl
    """
    global _pairing_code
    _pairing_code = _generar_pairing_code()

    # Detectar el conflicto de puerto ANTES de arrancar Flask, para no
    # dejar una copia con un codigo de emparejamiento que nadie valida.
    sonda = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sonda.bind(("0.0.0.0", puerto))
        sonda.listen(1)
    except OSError as e:
        sonda.close()
        raise RuntimeError(
            f"No se pudo iniciar el escáner móvil en el puerto {puerto}.\n"
            f"Probablemente ya hay otra copia de ExaStock abierta usando "
            f"el mismo puerto.\n\nDetalle: {e}"
        )
    sonda.close()

    error_caja = {}

    def _arranque():
        try:
            _app_flask.run(
                host="0.0.0.0", port=puerto, debug=False, use_reloader=False,
                ssl_context="adhoc",
            )
        except Exception as e:
            error_caja["error"] = e

    threading.Thread(target=_arranque, daemon=True).start()

    # Esperar hasta 5s: si el hilo de arranque reporta un error, fallar claro.
    for _ in range(50):
        if "error" in error_caja:
            raise RuntimeError(
                f"No se pudo iniciar el escáner móvil en el puerto {puerto}.\n"
                f"Probablemente ya hay otra copia de ExaStock abierta usando "
                f"el mismo puerto.\n\nDetalle: {error_caja['error']}"
            )
        time.sleep(0.1)

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
        # Si el codigo cambio (servidor reiniciado), actualizar la ventana.
        actual = obtener_pairing_code()
        if lbl_code.cget("text") != actual:
            lbl_code.configure(text=actual)
        ventana.after(300, _revisar_conexion)

    ventana.after(300, _revisar_conexion)

    return ventana
