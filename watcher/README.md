# Watcher de servers Quake 3

Proceso de fondo que acompaña a los servers dedicados (ioq3ded + CPMA). Lee el `qconsole.log` de cada server y:

## 1. Rotación manual con next / prev / volver

Cada vez que un server carga un mapa (por `vstr`, por la rotación de CPMA, un `callvote`, o un reinicio) pone por
rcon los alias calculados desde el mapa **real** que está corriendo:

| Comando | Qué hace |
|---|---|
| `/rcon vstr next` (o `siguiente`, `nextmap`) | pasa al próximo mapa de la lista |
| `/rcon vstr prev` (o `anterior`) | el anterior de la lista |
| `/rcon vstr volver` | vuelve al mapa en el que estabas antes, aunque hayas llegado por rotación o votación |
| `/rcon vstr v7` | salta al mapa número 7 |
| `/rcon actual` | dónde estás, ej. `basesiege[1/42]` |

La lista sale de `cpma/cfg-maps/nextmap_<modo>.cfg` (cadena `set v1 "map ..."`, `set v2 ...`). Se genera con
`generar_nextmap.py` o desde el [Q3 Map Config Generator](https://github.com/yimgame/Quake-3-Map-List-Config-Generator),
y `aplicar_nextmap.py` la carga en caliente. Cada server la ejecuta al arrancar con `+exec cfg-maps/nextmap_<modo>.cfg`.

## 2. AAS de bots automático

Si un server escribe `Fatal: aas file maps/X.aas is out of date` o `can't open maps/X.aas` (los bots no tienen
mapa de navegación), el watcher:

1. busca `X.bsp`, suelto en `baseq3/maps` o adentro de los pk3 de `baseq3`/`cpma` (gana el último en orden alfabético, como en el juego);
2. lo compila con **bspc** (`build_bspc.sh` lo arma desde [TTimo/bspc](https://github.com/TTimo/bspc));
3. deja `baseq3/maps/X.aas` suelto (en ioquake3 un archivo suelto le gana al del pk3, no hace falta tocar los pk3);
4. si el server sigue en ese mapa, lo recarga (`map_restart` no relee el AAS) y los bots ya navegan.

Hace **un intento por mapa** y anota el resultado en `tools/aas_generados.json`; para reintentar uno, borrá su entrada.

## 3. Salud de un server

Cada 30 s le pregunta `getstatus` a un server (por defecto CTF). Si no contesta 3 veces seguidas lo reinicia por ssh
con una key de forced command. Sin key, esta parte no hace nada.

## Configuración (variables de entorno)

| Variable | Por defecto | Qué es |
|---|---|---|
| `Q3_BASE` | `/game` | carpeta de Quake 3 (con `baseq3`, `cpma`, `instances`) |
| `Q3_SERVERS` | `ctf:27964,tdm:27963,ft:27966,ra3:27967,2v2:27962,ca:27965,ffa:27960,1v1:27961` | modo:puerto de cada server |
| `Q3_LOG` | `/instances/srv_{modo}/cpma/qconsole.log` | log de cada server (relativo a `Q3_BASE`) |
| `Q3_CFG` | `/cpma/cfg/yimq3a-cpma-{modo}-server-gemini.cfg` | cfg de cada server, de ahí sale la `rconpassword` |
| `Q3_CADENA` | `/cpma/cfg-maps/nextmap_{modo}.cfg` | lista next/prev |
| `Q3_RCON_HOST` | `127.0.0.1` | dónde escuchan los servers |
| `Q3_AAS` | `1` | `0` apaga la generación de AAS |
| `Q3_BSPC` | `$Q3_BASE/tools/bspc-ttimo` | binario de bspc |
| `Q3_SALUD_SERVER` / `Q3_SSH_KEY` / `Q3_SSH_HOST` | `ctf` / `/root/.ssh/watcher_key` / `192.168.0.200` | chequeo de salud y reinicio |

## Docker

```yaml
services:
  q3_watcher:
    image: python:3.13-slim
    network_mode: host          # rcon a los servers en 127.0.0.1
    volumes:
      - /ruta/a/Quake3:/game:rw
      - /ruta/a/q3a-server-launcher/watcher:/watcher:ro
    command: ["python3", "/watcher/q3_watcher.py"]
    restart: unless-stopped
```

Para el AAS: `bash watcher/build_bspc.sh /ruta/a/Quake3` una vez (en una máquina con la misma glibc que el container,
por ejemplo dentro de un `python:3.13-slim` con `apt install build-essential git`).

## IPs fijas de los master servers (2026-10-26)

ioq3 resuelve por DNS los `sv_master` en cada heartbeat (cada 5 min y en cada cambio de mapa) dentro del hilo del
juego: si el DNS tarda, el server se congela 0,5-6 s ("Hitch warning" en qconsole.log). Por eso las cfg usan
`seta sv_masterN "IP:puerto"   // nombre` y el watcher, cada 6 h y al arrancar (en un hilo aparte), resuelve los
nombres y, si la IP de la cfg ya no es una de las suyas, la cambia en las cfg y en vivo por rcon.

| Variable | Por defecto | Qué es |
|---|---|---|
| `Q3_MASTERS` | `master.quake3arena.com,directory.ioquake3.org,master.quakeservers.net:27950,master.maverickservers.com:27950,dpmaster.deathmask.net:27950` | nombres de sv_master1..N en orden (`""` apaga la tarea) |
| `Q3_MASTERS_CFGS` | `/cpma/cfg/*server*.cfg` | cfg donde están las líneas `seta sv_masterN` |
| `Q3_MASTERS_INTERVALO` | `21600` | cada cuántos segundos revisa |

## Modo de juego correcto (2026-10-26)

Cada server tiene su modo en la cfg (`seta mode_start "TDM"`). CPMA puede quedar en otro: por un voto de los
jugadores, o porque con `server_useMapModes 1` un mapa impone su modo (cure, pro-q3dm6... son "de 1v1"). Una vez
cambiado, CPMA lo arrastra de mapa en mapa y **solo vuelve reiniciando el server** (`game_restart` lo cuelga y
`map` no lo cambia). Por eso las cfg tienen `seta server_useMapModes 0` y el watcher, cada minuto, compara
`mode_current` con el `mode_start` de la cfg:

- **con humanos** no toca nada (si lo votaron, se respeta);
- **sin humanos** durante 2 revisiones seguidas, lo reinicia por el panel (`POST /api/reiniciar?modo=X`), como mucho
  una vez cada 15 min por server.

| Variable | Por defecto | Qué es |
|---|---|---|
| `Q3_MODO` | `1` | `0` apaga la tarea |
| `Q3_PANEL_URL` | `http://127.0.0.1:3020` | panel que hace el reinicio (tiene el socket de Docker) |
| `Q3_MODO_INTERVALO` | `60` | segundos entre revisiones |
| `Q3_MODO_VACIO_CHEQUEOS` | `2` | revisiones seguidas sin humanos antes de reiniciar |
| `Q3_MODO_ENFRIAMIENTO` | `900` | mínimo de segundos entre reinicios del mismo server |
