#!/usr/bin/env python3
"""
Watcher de los servers Quake 3 (CPMA/ioq3ded), parte de q3a-server-launcher. Corre con red host junto a los servers.
Toda la configuración sale de variables de entorno (ver watcher/README.md); los valores por defecto son los del
server donde nació (8 servers CPMA en 27960-27967, Quake 3 montado en /game).

1) Alias de rotación siempre al día, en los 8 servers: lee el qconsole.log de cada uno y, cada vez que carga un
   mapa (línea "Server: <mapa>" de ioquake3, venga de /rcon vstr next, de la rotación de CPMA, de un callvote o de
   un reinicio), pone por rcon: actual = mapa[N/total], mapa_actual, siguiente/next/nextmap = vN+1,
   anterior/prev = vN-1 y volver = el mapa REAL de antes. Así "vstr next/prev/volver" parten del mapa verdadero.
   La lista y el orden salen de cpma/cfg-maps/nextmap_<modo>.cfg (la cadena vstr v1..vN).

2) AAS de bots automático: si un server escribe "Fatal: aas file maps/X.aas is out of date" o "can't open
   maps/X.aas", busca X.bsp (suelto en baseq3/maps o adentro de los pk3 de baseq3/cpma, gana el último en orden como
   hace el juego), lo compila con bspc (tools/bspc-ttimo, github TTimo/bspc), deja maps/X.aas suelto en baseq3 (los
   archivos sueltos le ganan a los pk3 en ioq3) y, si el server sigue en ese mapa, lo recarga para que los bots lo
   tomen. Un intento por mapa: el resultado queda en tools/aas_generados.json (borrar la entrada para reintentar).

3) Salud del server CTF: cada HEALTHCHECK_INTERVAL s le manda "getstatus"; si no contesta varias veces seguidas
   lo reinicia por el canal SSH acotado (pct exec 101 -- docker restart quake3_ctf).

4) IPs fijas de los master servers: ioq3 resuelve por DNS los sv_master en CADA heartbeat (cada 5 min y en cada
   cambio de mapa) dentro del hilo del juego; si el DNS tarda (caché fría), el server se congela 0,5-6 s ("Hitch
   warning"). Por eso las cfg tienen `seta sv_masterN "IP:puerto"   // nombre`. Cada MASTERS_INTERVALO s (y al
   arrancar), en un hilo aparte, resuelve cada nombre; si la IP de la cfg ya no es una de las suyas, la cambia en
   todas las cpma/cfg/*server*.cfg y en vivo por rcon en todos los servers. Q3_MASTERS="" lo apaga.

5) Modo de juego correcto: cada server tiene su modo en la cfg (`seta mode_start "TDM"`). CPMA puede quedar en
   otro (un voto de los jugadores, o un mapa con modo propio si server_useMapModes vuelve a 1) y, una vez
   cambiado, lo arrastra de mapa en mapa: solo vuelve con un reinicio del server. Cada MODO_INTERVALO s compara
   mode_current con el de la cfg; si difiere y NO hay humanos (todos ping 0) durante MODO_VACIO_CHEQUEOS
   revisiones seguidas, lo reinicia por el panel (POST /api/reiniciar?modo=X, el panel tiene el socket de
   Docker), como mucho una vez cada MODO_ENFRIAMIENTO s por server. Con humanos no se toca: si lo votaron, se
   respeta hasta que el server quede vacío. Q3_MODO=0 lo apaga.

(Antes este script también forzaba la rotación de CTF, pero buscaba "Map:" -que ioq3 no escribe- y nunca actuó;
 CPMA rota bien solo, así que esa parte se sacó.)
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile

def _servers(texto):
    """"ctf:27964,tdm:27963" -> {"ctf": 27964, "tdm": 27963}"""
    return {m.strip(): int(p) for m, p in (x.split(":") for x in texto.split(",") if ":" in x)}


SERVERS = _servers(os.environ.get("Q3_SERVERS", "ctf:27964,tdm:27963,ft:27966,ra3:27967,2v2:27962,ca:27965,ffa:27960,1v1:27961"))
BASE = os.environ.get("Q3_BASE", "/game")                       # carpeta de Quake 3 (baseq3, cpma, instances)
LOG = BASE + os.environ.get("Q3_LOG", "/instances/srv_{modo}/cpma/qconsole.log")
CADENA = BASE + os.environ.get("Q3_CADENA", "/cpma/cfg-maps/nextmap_{modo}.cfg")
CFG = BASE + os.environ.get("Q3_CFG", "/cpma/cfg/yimq3a-cpma-{modo}-server-gemini.cfg")   # de acá sale la rconpassword
RCON_HOST = os.environ.get("Q3_RCON_HOST", "127.0.0.1")
SERVER_RE = re.compile(r"^Server: (\S+)\s*$")
AAS_RE = re.compile(r"(?:aas file maps/([^\s.]+)\.aas is out of date|can't open maps/([^\s.]+)\.aas)", re.I)

# --- AAS automático (Q3_AAS=0 lo apaga) ---
AAS_ACTIVO = os.environ.get("Q3_AAS", "1") != "0"
BSPC = os.environ.get("Q3_BSPC", BASE + "/tools/bspc-ttimo")   # ver watcher/build_bspc.sh
AAS_TRABAJO = BASE + "/tools/trabajo"
AAS_REGISTRO = BASE + "/tools/aas_generados.json"
AAS_DESTINO = BASE + "/baseq3/maps"
_aas_lock = threading.Lock()

# --- salud de un server (por defecto CTF): si no contesta, lo reinicia por ssh. Sin key no hace nada ---
SALUD_SERVER = os.environ.get("Q3_SALUD_SERVER", "ctf")
SSH_KEY_PATH = os.environ.get("Q3_SSH_KEY", "/root/.ssh/watcher_key")
SSH_HOST = os.environ.get("Q3_SSH_HOST", "192.168.0.200")      # forced command del lado del host: "restart-ctf"
HEALTHCHECK_INTERVAL = 30

# --- IPs fijas de los master servers (sv_master1..N, en ese orden) ---
MASTERS = [m.strip() for m in os.environ.get(
    "Q3_MASTERS", "master.quake3arena.com,directory.ioquake3.org,master.quakeservers.net:27950,"
                  "master.maverickservers.com:27950,dpmaster.deathmask.net:27950").split(",") if m.strip()]
MASTERS_CFGS = BASE + os.environ.get("Q3_MASTERS_CFGS", "/cpma/cfg/*server*.cfg")
MASTERS_INTERVALO = int(os.environ.get("Q3_MASTERS_INTERVALO", str(6 * 3600)))
# --- modo de juego correcto (ver 5) ---
MODO_ACTIVO = os.environ.get("Q3_MODO", "1") != "0"
PANEL_URL = os.environ.get("Q3_PANEL_URL", "http://127.0.0.1:3020")
MODO_INTERVALO = int(os.environ.get("Q3_MODO_INTERVALO", "60"))
MODO_VACIO_CHEQUEOS = int(os.environ.get("Q3_MODO_VACIO_CHEQUEOS", "2"))
MODO_ENFRIAMIENTO = int(os.environ.get("Q3_MODO_ENFRIAMIENTO", "900"))
MASTER_RE = re.compile(r'(?im)^\s*seta\s+sv_master(\d)\s+"([^":]+)(?::(\d+))?"[^\r\n]*')
HEALTHCHECK_FAILS_TO_RESTART = 3
RESTART_COOLDOWN = 90


def rcon(port, password, command):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.sendto(b"\xff\xff\xff\xffrcon " + password.encode() + b" " + command.encode(), (RCON_HOST, port))
        try:
            return s.recvfrom(16384)[0][4:].decode(errors="replace")
        except socket.timeout:
            return ""
    finally:
        s.close()


def vivo(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(b"\xff\xff\xff\xffgetstatus", (RCON_HOST, port))
        return s.recvfrom(4096)[0].startswith(b"\xff\xff\xff\xffstatusResponse")
    except OSError:
        return False
    finally:
        s.close()


def reiniciar_ctf():
    if not os.path.exists(SSH_KEY_PATH):
        print("[salud] no encuentro la key ssh, no puedo reiniciar", flush=True)
        return
    r = subprocess.run(["ssh", "-i", SSH_KEY_PATH, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                        "-o", "BatchMode=yes", f"root@{SSH_HOST}", "restart-ctf"], capture_output=True, text=True, timeout=30)
    print(f"[salud] reinicio pedido: exit={r.returncode} {r.stdout.strip()} {r.stderr.strip()[:120]}", flush=True)


class Server:
    def __init__(self, modo, port):
        self.modo, self.port = modo, port
        self.log = LOG.format(modo=modo)
        self.pos = os.path.getsize(self.log) if os.path.exists(self.log) else 0   # no releer historia vieja
        self.anterior = None          # mapa real de antes (para "volver")
        self.mapas, self.mtime_cadena, self.password = [], 0, ""

    def cargar(self):
        """Relee la cadena y la contraseña si cambiaron (por si se regenera nextmap_<modo>.cfg)."""
        ruta = CADENA.format(modo=self.modo)
        try:
            mt = os.path.getmtime(ruta)
        except OSError:
            return
        if mt != self.mtime_cadena:
            txt = open(ruta, errors="replace").read()
            pares = sorted((int(a), b) for a, b in re.findall(r'^set v(\d+) "(?:vstr aca ; )?map (\S+)', txt, re.M))
            self.mapas, self.mtime_cadena = [m for _, m in pares], mt
            m = re.search(r'rconpassword\s+"([^"]+)"', open(CFG.format(modo=self.modo), errors="replace").read())
            self.password = m.group(1) if m else ""
            print(f"[{self.modo}] {len(self.mapas)} mapas en la cadena", flush=True)

    def nuevos_mapas(self):
        """Eventos nuevos del log: ("mapa", nombre) cuando carga un mapa, ("aas", nombre) cuando a los bots les falta el AAS."""
        if not os.path.exists(self.log):
            return []
        tam = os.path.getsize(self.log)
        if tam < self.pos:                 # log nuevo (reinicio del server)
            self.pos = 0
        with open(self.log, "r", errors="replace") as f:
            f.seek(self.pos)
            lineas = f.readlines()
            self.pos = f.tell()
        eventos = []
        for l in lineas:
            m = SERVER_RE.match(l.strip())
            if m:
                eventos.append(("mapa", m.group(1)))
                continue
            a = AAS_RE.search(l)
            if a:
                eventos.append(("aas", a.group(1) or a.group(2)))
        return eventos

    def recargar_si_sigue(self, mapa):
        """Recarga el mapa para que el botlib lea el AAS nuevo (map_restart no lo relee). Con vstr si está en la cadena."""
        actual = self.mapa_corriendo()
        if not actual or actual.lower() != mapa.lower():
            print(f"[aas] {self.modo}: ya no está en {mapa} (está en {actual}), queda listo para la próxima", flush=True)
            return
        idx = next((i for i, x in enumerate(self.mapas) if x.lower() == mapa.lower()), None)
        rcon(self.port, self.password, f"vstr v{idx + 1}" if idx is not None else f"map {mapa}")
        print(f"[aas] {self.modo}: recargué {mapa} para que los bots usen el AAS nuevo", flush=True)

    def mapa_corriendo(self):
        r = rcon(self.port, self.password, "mapname")
        m = re.search(r'"mapname" is:"([^"^]+)', r)
        return m.group(1) if m else None

    def sincronizar(self, mapa, esperar=True):
        self.cargar()
        if not self.password or not self.mapas:
            return
        if esperar:
            time.sleep(3)                  # que termine de cargar el mapa antes de setear cvars
        n = len(self.mapas)
        idx = next((i for i, x in enumerate(self.mapas) if x.lower() == mapa.lower()), None)
        cmds = []
        if idx is not None:
            sig, ant = f"v{(idx + 1) % n + 1}", f"v{(idx - 1) % n + 1}"
            cmds += [f"set actual {self.mapas[idx]}[{idx + 1}/{n}]", f"set mapa_actual {self.mapas[idx]}",
                     f"set siguiente vstr {sig}", f"set next vstr {sig}", f"set nextmap vstr {sig}",
                     f"set anterior vstr {ant}", f"set prev vstr {ant}", f"set aca set volver vstr v{idx + 1}"]
        else:
            cmds += [f"set actual {mapa}[fuera_de_lista]", f"set mapa_actual {mapa}",
                     "set siguiente vstr v1", "set next vstr v1", "set nextmap vstr v1",
                     f"set anterior vstr v{n}", f"set prev vstr v{n}", 'set aca ""']
        ia = next((i for i, x in enumerate(self.mapas) if self.anterior and x.lower() == self.anterior.lower()), None)
        if ia is not None and (idx is None or ia != idx):
            cmds.append(f"set volver vstr v{ia + 1}")          # el mapa real de antes
        for c in cmds:                     # por rcon el ";" no separa comandos: uno por uno
            rcon(self.port, self.password, c)
        print(f"[{self.modo}] cargó {mapa} -> actual={idx + 1 if idx is not None else '?'}/{n}"
              f" volver={self.anterior or '-'}", flush=True)
        self.anterior = mapa


def _registro():
    try:
        return json.load(open(AAS_REGISTRO))
    except (OSError, ValueError):
        return {}


def buscar_bsp(mapa):
    """Devuelve (origen, bytes) del .bsp que usa el juego: suelto en baseq3/maps o el del pk3 que gana (último alfabético)."""
    suelto = os.path.join(AAS_DESTINO, mapa + ".bsp")
    if os.path.exists(suelto):
        return suelto, open(suelto, "rb").read()
    encontrado = None
    for carpeta in ("baseq3", "cpma"):
        for pk3 in sorted(glob.glob(f"{BASE}/{carpeta}/*.pk3") + glob.glob(f"{BASE}/{carpeta}/*.PK3"), key=str.lower):
            try:
                with zipfile.ZipFile(pk3) as z:
                    for n in z.namelist():
                        if n.lower() == f"maps/{mapa.lower()}.bsp":
                            encontrado = (pk3, n)
            except (zipfile.BadZipFile, OSError):
                pass
    if not encontrado:
        return None, None
    with zipfile.ZipFile(encontrado[0]) as z:
        return f"{os.path.basename(encontrado[0])}/{encontrado[1]}", z.read(encontrado[1])


def generar_aas(servidor, mapa):
    """Compila el AAS de un mapa (un intento por mapa) y recarga el mapa si el server sigue ahí."""
    with _aas_lock:
        reg = _registro()
        if mapa.lower() in reg:
            return
        reg[mapa.lower()] = {"estado": "compilando", "server": servidor.modo, "fecha": time.strftime("%Y-%m-%d %H:%M")}
        json.dump(reg, open(AAS_REGISTRO, "w"), indent=1)
    t0 = time.time()
    try:
        origen, datos = buscar_bsp(mapa)
        if not datos:
            raise RuntimeError("no encontré el .bsp en baseq3/maps ni en los pk3")
        os.makedirs(f"{AAS_TRABAJO}/maps", exist_ok=True)
        bsp = f"{AAS_TRABAJO}/maps/{mapa}.bsp"
        open(bsp, "wb").write(datos)
        print(f"[aas] {servidor.modo}: compilando {mapa}.aas desde {origen}…", flush=True)
        r = subprocess.run(["nice", "-n", "10", BSPC, "-bsp2aas", bsp, "-threads", "4", "-forcesidesvisible"], cwd=AAS_TRABAJO,
                           capture_output=True, text=True, timeout=900)
        aas = bsp[:-4] + ".aas"
        if r.returncode != 0 or not os.path.exists(aas):
            raise RuntimeError(f"bspc falló (exit {r.returncode}): {(r.stdout + r.stderr)[-200:]}")
        areas = (re.findall(r"numareas = (\d+)", r.stdout) or ["?"])[-1]
        shutil.copyfile(aas, os.path.join(AAS_DESTINO, mapa + ".aas"))
        os.remove(bsp); os.remove(aas)
        estado = {"estado": "ok", "origen": origen, "areas": areas, "segundos": round(time.time() - t0)}
        print(f"[aas] {servidor.modo}: {mapa}.aas listo ({areas} áreas, {estado['segundos']}s)", flush=True)
    except Exception as e:
        estado = {"estado": "error", "detalle": str(e)[:300]}
        print(f"[aas] {servidor.modo}: no pude generar {mapa}.aas: {e}", flush=True)
    with _aas_lock:
        reg = _registro()
        reg[mapa.lower()] = dict(reg.get(mapa.lower(), {}), **estado)
        json.dump(reg, open(AAS_REGISTRO, "w"), indent=1)
    if estado["estado"] == "ok":
        try:
            servidor.recargar_si_sigue(mapa)
        except Exception as e:
            print(f"[aas] {servidor.modo}: no pude recargar {mapa}: {e}", flush=True)


def actualizar_masters(servers):
    """Resuelve los nombres de los masters y, si alguno cambió de IP, lo cambia en las cfg y en vivo."""
    cambios = {}
    for i, entrada in enumerate(MASTERS, start=1):
        nombre, _, puerto = entrada.partition(":")
        try:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(nombre, int(puerto or 27950), socket.AF_INET,
                                                               socket.SOCK_DGRAM)})
        except OSError as e:
            print(f"[masters] no pude resolver {nombre}: {e} (dejo la IP que estaba)", flush=True)
            continue
        cambios[i] = (nombre, ips, puerto or "27950")
    modificados = {}
    for archivo in glob.glob(MASTERS_CFGS):
        try:
            texto = open(archivo, encoding="latin1").read()
        except OSError:
            continue
        nuevo = texto
        for m in MASTER_RE.finditer(texto):
            i = int(m.group(1))
            if i not in cambios or m.group(2) in cambios[i][1]:
                continue                                    # sin datos, o la IP sigue siendo válida
            nombre, ips, puerto = cambios[i]
            nuevo = nuevo.replace(m.group(0), f'seta sv_master{i} "{ips[0]}:{puerto}"   // {nombre}')
            modificados[i] = (nombre, m.group(2), ips[0], puerto)
        if nuevo != texto:
            with open(archivo, "r+", encoding="latin1") as f:   # r+ para no cambiarle el dueño en el share
                f.write(nuevo)
                f.truncate()
    for i, (nombre, vieja, ip, puerto) in modificados.items():
        print(f"[masters] {nombre}: {vieja} -> {ip}", flush=True)
        for s in servers:
            if s.password:
                rcon(s.port, s.password, f"set sv_master{i} {ip}:{puerto}")
    if not modificados:
        print(f"[masters] IPs al día ({len(cambios)}/{len(MASTERS)} resueltas)", flush=True)


def estado_modo(port):
    """(mode_current, humanos) por getstatus; (None, None) si no contesta."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.5)
    try:
        sock.sendto(b"\xff\xff\xff\xffgetstatus\n", (RCON_HOST, port))
        datos = sock.recv(65535).decode("latin1")
    except OSError:
        return None, None
    finally:
        sock.close()
    lineas = datos.split("\n")
    partes = lineas[1].split("\\")[1:] if len(lineas) > 1 else []
    info = dict(zip(partes[0::2], partes[1::2]))
    humanos = sum(1 for l in lineas[2:] if re.match(r'^-?\d+ [1-9]\d* "', l.strip()))
    return info.get("mode_current"), humanos


def modo_esperado(modo):
    try:
        m = re.search(r'(?im)^\s*seta?\s+mode_start\s+"?([^"\s]+)', open(CFG.format(modo=modo), errors="replace").read())
        return m.group(1).upper() if m else None
    except OSError:
        return None


def vigilar_modos(servers):
    vacios, ultimo_reinicio, avisado = {}, {}, {}
    while True:
        time.sleep(MODO_INTERVALO)
        for s in servers:
            try:
                esperado = modo_esperado(s.modo)
                actual, humanos = estado_modo(s.port)
                if not esperado or actual is None:          # sin modo en la cfg, o CPMA no lo informa (RA3)
                    continue
                if actual.upper() == esperado:
                    vacios[s.modo] = 0
                    avisado.pop(s.modo, None)
                    continue
                if humanos:
                    vacios[s.modo] = 0
                    if not avisado.get(s.modo):
                        print(f"[modo] {s.modo}: está en {actual} (debería {esperado}) pero hay {humanos} humano(s): "
                              f"lo respeto hasta que quede vacío", flush=True)
                        avisado[s.modo] = True
                    continue
                vacios[s.modo] = vacios.get(s.modo, 0) + 1
                if vacios[s.modo] < MODO_VACIO_CHEQUEOS:
                    continue
                if time.time() - ultimo_reinicio.get(s.modo, 0) < MODO_ENFRIAMIENTO:
                    continue
                print(f"[modo] {s.modo}: está en {actual} y debería {esperado}, sin humanos: lo reinicio", flush=True)
                ultimo_reinicio[s.modo] = time.time()
                vacios[s.modo] = 0
                import urllib.request
                req = urllib.request.Request(f"{PANEL_URL}/api/reiniciar?modo={s.modo}", method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    print(f"[modo] {s.modo}: {r.read().decode()[:120]}", flush=True)
            except Exception as e:
                print(f"[modo] {s.modo}: error: {e}", flush=True)


def vigilar_masters(servers):
    while True:
        try:
            actualizar_masters(servers)
        except Exception as e:
            print(f"[masters] error: {e}", flush=True)
        time.sleep(MASTERS_INTERVALO)


def main():
    servers = [Server(m, p) for m, p in SERVERS.items()]
    for s in servers:
        s.cargar()
        try:
            actual = s.mapa_corriendo()
            if actual:
                s.sincronizar(actual, esperar=False)
        except Exception as e:
            print(f"[{s.modo}] no pude leer el mapa actual: {e}", flush=True)
    print("[watcher] sincronizando alias en", ", ".join(SERVERS), flush=True)
    if MASTERS:
        threading.Thread(target=vigilar_masters, args=(servers,), daemon=True).start()
    if MODO_ACTIVO:
        threading.Thread(target=vigilar_modos, args=(servers,), daemon=True).start()
    fallos, ultimo_chequeo, gracia = 0, 0.0, 0.0
    while True:
        time.sleep(2)
        for s in servers:
            try:
                for tipo, mapa in s.nuevos_mapas():
                    if tipo == "mapa":
                        s.sincronizar(mapa)
                    elif AAS_ACTIVO and mapa.lower() not in _registro() and os.path.exists(BSPC):
                        threading.Thread(target=generar_aas, args=(s, mapa), daemon=True).start()
            except Exception as e:
                print(f"[{s.modo}] error: {e}", flush=True)
        ahora = time.time()
        if ahora >= gracia and ahora - ultimo_chequeo >= HEALTHCHECK_INTERVAL:
            ultimo_chequeo = ahora
            if SALUD_SERVER not in SERVERS or vivo(SERVERS[SALUD_SERVER]):
                fallos = 0
            else:
                fallos += 1
                print(f"[salud] {SALUD_SERVER} no responde ({fallos}/{HEALTHCHECK_FAILS_TO_RESTART})", flush=True)
                if fallos >= HEALTHCHECK_FAILS_TO_RESTART:
                    reiniciar_ctf()
                    fallos, gracia = 0, ahora + RESTART_COOLDOWN


if __name__ == "__main__":
    main()
