# HolyArrs stack

An automated deploy of [HolyArrs](https://github.com/HolyArrs/HolyArrs) with qBittorrent
and Jellyfin on one Docker host. `make up` starts the three apps and connects them, so
HolyArrs can search, download and import right away.

- **HolyArrs** finds movies and shows, sends them to qBittorrent, imports them into the
  library, and handles requests and subtitles.
- **qBittorrent** downloads.
- **Jellyfin** plays the library.

Use it only with content you have the right to download and share.

## What the host needs

- Linux with Docker Engine and Docker Compose v2, plus `make` and `git`.
- One folder, `/data` by default, holding both downloads and media on a single
  filesystem. Finished downloads then become hardlinks: instant and no extra space.
- Free ports: 6262 (HolyArrs), 8080 (qBittorrent), 8096 (Jellyfin) and the torrent port
  (6881 by default).

Nothing here installs packages, mounts disks or changes the host. `make` only checks.

## Install

```sh
git clone https://github.com/venzellucas/arr-stack.git
cd arr-stack
sudo mkdir -p /data            # or point DATA_DIR at your own folder in .env
make up
```

Settings come only from `.env`. Without one, every default in `.env.example` applies. To
change a setting, copy `.env.example` to `.env` and keep only the lines you change.

## What `make up` does

1. Checks that Docker and Compose work and that the data folder is writable and on one
   filesystem.
2. Makes `downloads`, `media/movies` and `media/tv` in the data folder, and gives a new
   qBittorrent a generated password with downloads going to `/data/downloads`.
3. Starts the containers and waits until each app answers.
4. Connects them:
   - Jellyfin gets its admin, the Movies and TV libraries, and an API key for HolyArrs.
   - HolyArrs gets its owner, its library folders, qBittorrent as its Downloader,
     Jellyfin as its media server, and the built-in Knaben and TorrentsCSV sources.
     qBittorrent and Jellyfin must pass HolyArrs' own Test. A torrent site that does
     not answer at that moment only gets a warning.

The generated passwords and keys are saved in
`<CONFIG_DIR>/stack-setup/credentials.json`, readable by root only. `make credentials`
shows the path. Sign in to each app with them, and change them if you like.

Running `make up` again is safe. Once an app is connected it is marked done in that file
and never touched again, so later changes you make in its settings stay. A run that
failed halfway picks up where it stopped. An app that was set up before this script, or
whose generated password you changed before the connection finished, is reported and
skipped: connect it in HolyArrs' Settings yourself. A refused password is remembered, and
later runs no longer sign in to that app, since repeated failed sign-ins can lock an
account.

## What stays manual

Your own taste: quality and size rules, subtitle languages and subtitle sites,
more torrent sites (link Prowlarr or Jackett in HolyArrs' Settings > Sources), and
Jellyfin's playback preferences.

## Commands

| Command | What it does |
|---|---|
| `make up` | Check, start or update the containers, connect what is new |
| `make check` | Check the host and that every app answers |
| `make update` | Pull new images now and restart the apps that changed |
| `make credentials` | Show where the generated passwords are |
| `make logs` | Follow the logs |
| `make down` | Stop and remove the containers; configuration and media stay |
| `make test` | Run the setup script's unit tests (in a container) |

App configuration lives in `CONFIG_DIR` (`/opt/appdata` by default), one folder per app.
Back up that folder. Backing up the media in `DATA_DIR` is up to you.

## Optional parts

**Weekly updates.** Add `COMPOSE_PROFILES=updater` to `.env` and run `make up`. The
`media-updater` container then pulls new images for the three apps on `UPDATE_SCHEDULE`
(Sundays at 05:00 by default, in `TZ`) and restarts only the apps whose image changed.
It needs the Docker socket to do that, which gives it control of the host's Docker.
Remove the line and run `make up` to turn it off. HolyArrs backs up its own data before
an update that changes it; see its
[Updates guide](https://github.com/HolyArrs/HolyArrs/blob/main/docs/run/updates.md).
Updates of the host itself, including Docker, are outside this stack.

**Hardware transcoding.** If the host has `/dev/dri/renderD128`, set in `.env`:

```sh
COMPOSE_FILE=docker-compose.yml:compose.gpu.yml
RENDER_GID=<output of: stat -c %g /dev/dri/renderD128>
```

**Reverse proxy.** HolyArrs checks the host name and the client address of each request.
Behind a reverse proxy, set `HOLYARRS_HOST` to the name browsers use and `REVERSE_PROXY`
to the proxy's address.
