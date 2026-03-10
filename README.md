# Minecraft Manager

A self-hosted, cross-platform Minecraft server manager built with Python and Flask.

## What it does

- Secure single-user login with hashed password
- Install Paper, Fabric, or Spigot into a local server folder
- Start/stop the server and send live console commands
- WebSocket-based live console viewer
- Plugin upload / URL download / deletion
- World backup, import, and deletion tools
- `server.properties` editor in the browser
- Log viewing via the web UI
- Configurable Java path and RAM allocation

## Important note on world editing

This project includes **world management** (backup/import/delete) and configuration editing, but not a full in-browser voxel editor. A true browser-based block/chunk world editor is a much larger subsystem and would normally require a dedicated rendering/editing stack. This codebase is a solid foundation for that, but it is not a full visual MCEdit-style replacement yet.

## Supported install flow

- **Paper**: downloads the latest stable build for the version you choose
- **Fabric**: downloads the Fabric server launcher for the version you choose
- **Spigot**: downloads `BuildTools.jar`; you then run BuildTools from the UI to compile the server jar locally

## Requirements

- Python 3.11+
- Java installed and reachable from `java` or a custom path you configure in the app
- Git installed if you want to build Spigot with BuildTools

## Run locally

```bash
cd minecraft_manager
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:8080`.

## Security recommendations

For private remote access, put the app behind a reverse proxy with HTTPS and IP restrictions or a VPN. The app has a secure password flow, but transport security should still be handled with TLS when exposed outside your machine.

## Next upgrades worth adding

- TOTP / WebAuthn 2FA
- Multiple server instances
- Automatic backups and scheduled restarts
- Plugin marketplace metadata
- NBT / whitelist / ops editors
