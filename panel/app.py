#!/usr/bin/env python3
"""
Panel unificado de los servers de Quake 3 (q3_server_tools). Solo biblioteca estándar de Python.

Solapas: Servers (estado en vivo de los 8 fijos + los bajo demanda, humanos/bots, reiniciar, next/prev),
Launcher y Generador (las apps existentes embebidas), Watcher (estado, log, AAS generados, masters),
FastDL (estado, pk3 servidos, prueba de descarga) y Compose (los stacks de este repo).

Habla con Docker por el socket (montado en /var/run/docker.sock) y con los servers por UDP (getstatus / rcon).
La rcon password se lee de la cfg de cada server (igual que el watcher): nunca está en este código.
Pensado para la LAN (sin login, igual que el launcher): no publicarlo a internet.
"""
import glob
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.environ.get("Q3_BASE", "/game")
HOST = os.environ.get("Q3_HOST", "192.168.0.202")
PUERTO = int(os.environ.get("PANEL_PORT", "3020"))
REPO = os.environ.get("REPO_DIR", "/repo")
CFG = BASE + "/cpma/cfg/yimq3a-cpma-{modo}-server-gemini.cfg"
FASTDL_PUBLICO = os.environ.get("FASTDL_URL", "http://q3dl.thrallguards.com.ar")
LAUNCHER_URL = os.environ.get("LAUNCHER_URL", f"http://{HOST}:3010")   # nombre largo y miniatura de cada mapa
ESTATICOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# los 8 servers fijos: modo -> (puerto, container)
FIJOS = {
    "ffa": (27960, "quake3_ffa"), "1v1": (27961, "quake3_1v1"), "2v2": (27962, "quake3_2v2"),
    "tdm": (27963, "quake3_tdm"), "ctf": (27964, "quake3_ctf"), "ca": (27965, "quake3_clan_arena"),
    "ft": (27966, "quake3_freeze_tag"), "ra3": (27967, "quake3_rocket_arena"),
}
BAJO_DEMANDA = range(28060, 28068)
# containers que el panel puede reiniciar (nada más)
REINICIABLES = {c for _, c in FIJOS.values()} | {"quake3_ctf_nextmap_watcher", "q3-fastdl", "q3-launcher",
                                                   "q3-map-config-generator"}
COLOR_RE = re.compile(r"\^(x[0-9a-fA-F]{6}|.)")


def limpio(s):
    return COLOR_RE.sub("", s or "")


# ------------------------------------------------------------------ Docker por el socket
class _UnixConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=20)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect("/var/run/docker.sock")


def docker(metodo, ruta, crudo=False):
    c = _UnixConn()
    try:
        c.request(metodo, ruta)
        r = c.getresponse()
        datos = r.read()
        if r.status >= 400:
            raise RuntimeError(f"docker {r.status}: {datos[:200]!r}")
        return datos if crudo else (json.loads(datos) if datos else None)
    finally:
        c.close()


def docker_logs(nombre, lineas=150):
    b = docker("GET", f"/containers/{nombre}/logs?stdout=1&stderr=1&tail={lineas}&timestamps=1", crudo=True)
    out, i = [], 0
    while i + 8 <= len(b):                         # flujo multiplexado de Docker (sin TTY)
        n = int.from_bytes(b[i + 4:i + 8], "big")
        out.append(b[i + 8:i + 8 + n])
        i += 8 + n
    texto = b"".join(out).decode("utf-8", "replace") if out else b.decode("utf-8", "replace")
    return texto.splitlines()


def estado_containers():
    try:
        cs = docker("GET", "/containers/json?all=1")
    except Exception:
        return {}
    res = {}
    for c in cs:
        nombre = c["Names"][0].lstrip("/")
        res[nombre] = {"estado": c["State"], "detalle": c["Status"], "imagen": c["Image"]}
    return res


# ------------------------------------------------------------------ Quake 3 por UDP
def udp(puerto, mensaje, espera=1.2):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(espera)
    try:
        s.sendto(b"\xff\xff\xff\xff" + mensaje, (HOST, puerto))
        return s.recv(65535).decode("latin1")
    except OSError:
        return None
    finally:
        s.close()


def getstatus(puerto):
    t0 = time.time()
    r = udp(puerto, b"getstatus\n")
    if not r:
        return None
    lineas = r.split("\n")
    partes = lineas[1].split("\\")[1:] if len(lineas) > 1 else []
    info = dict(zip(partes[0::2], partes[1::2]))
    jugadores = []
    for l in lineas[2:]:
        m = re.match(r'^(-?\d+) (\d+) "(.*)"$', l.strip())
        if m:
            ping = int(m.group(2))
            jugadores.append({"nombre": limpio(m.group(3)), "score": int(m.group(1)), "ping": ping,
                              "bot": ping == 0})
    jugadores.sort(key=lambda j: (j["bot"], -j["score"]))
    return {"puerto": puerto, "host": limpio(info.get("sv_hostname", "")), "mapa": info.get("mapname", ""),
            "max": int(info.get("sv_maxclients", "0") or 0), "jugadores": jugadores,
            "humanos": sum(1 for j in jugadores if not j["bot"]), "bots": sum(1 for j in jugadores if j["bot"]),
            "respuesta_ms": round((time.time() - t0) * 1000)}


def rcon_password(modo):
    try:
        m = re.search(r'rconpassword\s+"([^"]+)"', open(CFG.format(modo=modo), errors="replace").read(), re.I)
        return m.group(1) if m else ""
    except OSError:
        return ""


def rcon(modo, comando):
    puerto = FIJOS[modo][0]
    pw = rcon_password(modo)
    if not pw:
        raise RuntimeError("no encontré la rcon password en la cfg")
    r = udp(puerto, f"rcon {pw} {comando}".encode(), 2)
    return (r or "").replace("\xff\xff\xff\xffprint\n", "").strip()


# ------------------------------------------------------------------ datos de cada solapa
def mapas_launcher():
    """puerto -> {longname, levelshot} desde el launcher (mismo dato que muestra él). Si no contesta, nada."""
    try:
        with urllib.request.urlopen(LAUNCHER_URL + "/api/servers", timeout=3) as r:
            return {int(x["port"]): {"longname": x.get("longname") or "", "levelshot": x.get("levelshot") or ""}
                    for x in json.loads(r.read()) if x.get("port")}
    except Exception:
        return {}


def api_servers():
    cont = estado_containers()
    extra = mapas_launcher()
    resultado = {"fijos": [], "bajo_demanda": []}
    hilos, datos = [], {}

    def consultar(p):
        datos[p] = getstatus(p)
    for p in [v[0] for v in FIJOS.values()] + list(BAJO_DEMANDA):
        h = threading.Thread(target=consultar, args=(p,))
        h.start()
        hilos.append(h)
    for h in hilos:
        h.join()
    for p, e in datos.items():
        if e:
            info = extra.get(p, {})
            e["longname"] = info.get("longname") if info.get("longname") and info.get("longname") != e["mapa"] else ""
            e["levelshot"] = LAUNCHER_URL + info["levelshot"] if info.get("levelshot") else ""
    for modo, (p, nombre) in FIJOS.items():
        resultado["fijos"].append({"modo": modo, "container": nombre, "docker": cont.get(nombre),
                                   "estado": datos.get(p)})
    for p in BAJO_DEMANDA:
        if datos.get(p):
            resultado["bajo_demanda"].append({"puerto": p, "estado": datos[p]})
    return resultado


def api_watcher():
    nombre = "quake3_ctf_nextmap_watcher"
    cont = estado_containers().get(nombre)
    try:
        log = docker_logs(nombre, 200)
    except Exception as e:
        log = [f"(no pude leer el log: {e})"]
    try:
        aas = json.load(open(BASE + "/tools/aas_generados.json"))
    except Exception:
        aas = {}
    aas_lista = sorted(({"mapa": k, **(v if isinstance(v, dict) else {"resultado": v})} for k, v in aas.items()),
                       key=lambda x: str(x.get("cuando") or x.get("fecha") or ""), reverse=True)
    masters = []
    try:
        texto = open(CFG.format(modo="ctf"), errors="replace").read()
        masters = [{"n": m.group(1), "ip": m.group(2), "nombre": (m.group(3) or "").strip()} for m in
                   re.finditer(r'(?im)^\s*seta\s+sv_master(\d)\s+"([^"]+)"\s*(?://\s*(.*))?$', texto)]
    except OSError:
        pass
    return {"docker": cont, "log": log, "aas": aas_lista[:60], "aas_total": len(aas), "masters": masters}


def api_fastdl():
    cont = estado_containers().get("q3-fastdl")
    pk3 = glob.glob(BASE + "/baseq3/*.pk3")
    total = sum(os.path.getsize(f) for f in pk3 if os.path.exists(f))
    recientes = sorted(pk3, key=lambda f: os.path.getmtime(f), reverse=True)[:12]
    prueba = {}
    if recientes:
        archivo = os.path.basename(recientes[0])
        for etiqueta, url in (("LAN", f"http://{HOST}:8091/baseq3/{archivo}"),
                              ("Público", f"{FASTDL_PUBLICO}/baseq3/{archivo}")):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "q3-panel"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    prueba[etiqueta] = {"url": url, "codigo": r.status, "ms": round((time.time() - t0) * 1000)}
            except Exception as e:
                prueba[etiqueta] = {"url": url, "error": str(e)[:120]}
    try:
        log = [l for l in docker_logs("q3-fastdl", 300) if ".pk3" in l][-40:]
    except Exception:
        log = []
    return {"docker": cont, "pk3": len(pk3), "total_gb": round(total / 2 ** 30, 2),
            "recientes": [{"archivo": os.path.basename(f), "mb": round(os.path.getsize(f) / 2 ** 20, 1),
                           "fecha": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f)))}
                          for f in recientes], "prueba": prueba, "log": log}


def api_compose():
    archivos = sorted(glob.glob(REPO + "/docker-compose.yml") + glob.glob(REPO + "/stacks/*.yml"))
    return [{"archivo": os.path.relpath(f, REPO), "texto": open(f, errors="replace").read()} for f in archivos]


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, datos, codigo=200):
        b = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        ruta = urlparse(self.path).path
        try:
            if ruta == "/api/servers":
                return self._json(api_servers())
            if ruta == "/api/watcher":
                return self._json(api_watcher())
            if ruta == "/api/fastdl":
                return self._json(api_fastdl())
            if ruta == "/api/compose":
                return self._json(api_compose())
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        archivo = "index.html" if ruta in ("/", "/index.html") else None
        if not archivo:
            self.send_error(404)
            return
        b = open(os.path.join(ESTATICOS, archivo), "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/reiniciar":
                # por container, o por modo (lo usa el watcher para devolver un server vacío a su modo)
                nombre = q.get("container", "") or FIJOS.get(q.get("modo", ""), (0, ""))[1]
                if nombre not in REINICIABLES:
                    return self._json({"error": "ese container no se reinicia desde acá"}, 400)
                docker("POST", f"/containers/{nombre}/restart?t=5")
                return self._json({"ok": True, "mensaje": f"{nombre} reiniciado"})
            if u.path == "/api/mapa":
                modo, accion = q.get("modo", ""), q.get("accion", "")
                comandos = {"siguiente": "vstr next", "anterior": "vstr prev", "volver": "vstr volver",
                            "reiniciar": "map_restart"}
                if modo not in FIJOS or accion not in comandos:
                    return self._json({"error": "pedido inválido"}, 400)
                return self._json({"ok": True, "respuesta": rcon(modo, comandos[accion])})
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self.send_error(404)


if __name__ == "__main__":
    print(f"Panel Q3 en http://0.0.0.0:{PUERTO}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Handler).serve_forever()
