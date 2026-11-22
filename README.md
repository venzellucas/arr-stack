# media stack

[HolyArrs](https://github.com/HolyArrs/HolyArrs), qBittorrent and Jellyfin on one
Docker host, kept up to date by a small updater container.

- **HolyArrs** finds movies and shows, sends them to qBittorrent, imports them into
  the library, handles requests, and looks for subtitles.
- **qBittorrent** downloads.
- **Jellyfin** plays the library.

Use it only with content you have the right to download and share.

## What the host needs

- Linux with Docker Engine and Docker Compose v2.
- One folder, `/data` by default, holding both downloads and media on a single
  filesystem. Finished downloads then become hardlinks: instant and no extra space.
  A typical layout is `/data/downloads` and `/data/media/{movies,tv}`.
- `make`, `git`, and a free set of ports: 6262 (HolyArrs), 8080 (qBittorrent),
  8096 (Jellyfin) and the torrent port (6881 by default).
- Optional: `/dev/dri/renderD128` for Jellyfin hardware transcoding. When it exists,
  `make` adds `compose.gpu.yml` and passes the device's group automatically.

Nothing here installs packages, mounts disks or changes the host. `make` only checks.

## Install

```sh
git clone https://github.com/venzellucas/arr-stack.git
cd arr-stack
make up
```

`make up` checks the host, starts the containers and waits until each app answers.
No configuration file is needed: every setting has a default. To change one, copy
`.env.example` to `.env` and keep only the lines you change.

| Command | What it does |
|---|---|
| `make up` | Check the host, start or update the containers, wait until healthy |
| `make check` | Check the host and that every app answers |
| `make update` | Pull new images now and restart the apps that changed |
| `make logs` | Follow the logs |
| `make down` | Stop and remove the containers; configuration and media stay |

App configuration lives in `CONFIG_DIR` (`/opt/appdata` by default), one folder per
app. Back up that folder; the media in `DATA_DIR` is yours to back up or not.

## First run

Open each app once, in this order.

1. **qBittorrent**, `http://<host>:8080`. The first password is temporary: find it
   with `docker logs qbittorrent`. Sign in, set your own password under
   **Options > Web UI**, and set the default save path to `/data/downloads` under
   **Options > Downloads**.
2. **Jellyfin**, `http://<host>:8096`. Make the admin account and add the libraries
   `/data/media/movies` and `/data/media/tv`. Under **Dashboard > API keys**, make a
   key for HolyArrs.
3. **HolyArrs**, `http://<host>:6262`. Make the owner account right away: until you
   do, whoever opens it first becomes the owner. Then, in **Settings**:
   - **Downloaders:** qBittorrent at `http://qbittorrent:8080` with the password from
     step 1.
   - **Media servers:** Jellyfin at `http://jellyfin:8096` with the key from step 2.
   - **Sources:** add the built-in sites, such as Knaben and TorrentsCSV. Prowlarr or
     Jackett can be linked later for other sites.
   - **Library folders:** `/data/media/movies` and `/data/media/tv`.
   - **Subtitles:** pick your languages. Subtitle search is a held-back HolyArrs
     feature; set `HOLYARRS_FLAGS=subtitle_sites` in `.env`, run `make up`, then add
     subtitle sites (OpenSubtitles.com and subdl need a free account).

The apps reach each other by container name on the `media` network.

## Updates

The `media-updater` container pulls new images for HolyArrs, qBittorrent and
Jellyfin on `UPDATE_SCHEDULE` (Sundays at 05:00 by default, in `TZ`) and restarts
only the apps whose image changed. It needs the Docker socket to do that, which
gives it control of the host's Docker. It never updates itself; `make up` does.

HolyArrs backs up its own data before an update that changes it, and can go back to
the older version; see its [Updates guide](https://github.com/HolyArrs/HolyArrs/blob/main/docs/run/updates.md).
Updates of the host itself, including Docker, are outside this stack.

## Reverse proxy

HolyArrs checks the host name and the client address of each request. Behind a
reverse proxy, set `HOLYARRS_HOST` to the name browsers use and `REVERSE_PROXY` to
the proxy's address in `.env`, then run `make up`.
