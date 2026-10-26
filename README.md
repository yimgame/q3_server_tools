# q3_server_tools

Herramientas que acompañan a los servers de Quake 3 (ioq3ded + CPMA) de **The Meg & Yim**: todo lo que corre al
lado de los servers para que anden solos. Viven en el LXC 101 (Docker, administrado con Portainer) y leen/escriben
la carpeta del juego `\\192.168.0.200\gamesrv\Quake3` (`/mnt/ssd-jellyfin/gamesrv/Quake3`).

## Panel unificado → http://192.168.0.202:3020

Una sola página con solapas: **Servers** (los 8 fijos y los bajo demanda en vivo: mapa, jugadores 👤 humanos /
🤖 bots, siguiente/anterior/volver de mapa, reiniciar), **Launcher** y **Generador** (las apps de siempre, embebidas),
**Watcher** (estado, log, AAS generados, IPs de los masters), **FastDL** (pk3 servidos, prueba de descarga LAN y
pública, últimas descargas) y **Compose** (los composes de este repo). Código en [`panel/`](panel/app.py): Python
sin dependencias, habla con Docker por el socket y con los servers por UDP. Solo LAN (sin login, como el launcher).

## Qué hay acá

| Carpeta | Qué es | Cómo corre |
|---|---|---|
| [`watcher/`](watcher/README.md) | **Watcher**: alias de rotación next/prev/volver en los 8 servers, AAS de bots automático con bspc, salud del CTF (lo reinicia si no contesta) e IPs fijas de los master servers (evita los cortes "Hitch warning") | container `quake3_ctf_nextmap_watcher` del stack `quake3_server`, monta esta carpeta en `/watcher` (solo lectura) |
| [`stacks/quake3-servers.yml`](stacks/quake3-servers.yml) | Los 8 servers fijos (FFA 27960, 1v1 27961, 2v2 27962, TDM 27963, CTF 27964, CA 27965, FT 27966, RA3 27967) + el watcher | stack Portainer **34** `quake3_server` |
| [`stacks/q3-fastdl.yml`](stacks/q3-fastdl.yml) | FastDL: nginx que sirve los `.pk3` (`sv_dlURL` = `http://q3dl.thrallguards.com.ar`) | stack Portainer **40** `q3-fastdl` |
| [`stacks/q3-map-config-generator.yml`](stacks/q3-map-config-generator.yml) | Generador de listas de mapas / `nextmap_<modo>.cfg` (:5050) | stack Portainer **39** |
| [`stacks/q3-launcher.yml`](stacks/q3-launcher.yml) | Launcher web (:3010), servers bajo demanda 28060-28067 | container suelto (el stack 41 de Portainer quedó desactualizado) |
| [`panel/`](panel/app.py) + [`stacks/q3-panel.yml`](stacks/q3-panel.yml) | Panel unificado (:3020) | stack Portainer **58** `q3-panel` |
| [`docker-compose.yml`](docker-compose.yml) | **Todo el proyecto en un solo compose** (13 servicios), para migrar a un único stack | todavía no desplegado así |

Los `stacks/*.yml` son copia de lo que tiene Portainer (la fuente de verdad es Portainer: si se cambia allá,
actualizar acá y viceversa).

## Repos relacionados (no se copian acá)

- **Launcher**: [yimgame/q3a-server-launcher](https://github.com/yimgame/q3a-server-launcher) (`projects/q3_server_launcher`).
- **Generador de configs**: [yimgame/Quake-3-Map-List-Config-Generator](https://github.com/yimgame/Quake-3-Map-List-Config-Generator) (`projects/q3-map-config-generator`).
- **Sitio Yim Game** (ranking, partidas, demos): [yimgame/Gaming-Platform](https://github.com/yimgame/Gaming-Platform).
- **Aviso por Telegram cuando entra un humano** (`q3_humanos.py`) y el monitoreo general: repo de administración del
  homelab (LXC 104), porque usan sus credenciales.

## Secretos

Nada de claves en este repo. La rcon password está en las cfg del juego (`cpma/cfg/*server*.cfg`) y en los `.env`
de quien la usa; el watcher la lee de la cfg. La key SSH del watcher (`Quake3/scripts/pve_ctf_watcher_key`) es de un
forced command del host que solo puede reiniciar el server CTF; nunca va al repo (`.gitignore`).

## Historia

- 2026-10-26: se crea este repo; el watcher se mueve acá desde `q3_server_launcher/watcher`; panel unificado (:3020);
  compose unificado validado con `docker compose config`.
- Los scripts viejos de `Quake3/scripts` (`ctf_nextmap_watcher.py`, `generar_nextmap.py`, `aplicar_nextmap.py`)
  quedaron reemplazados por el watcher y ya no corren.
