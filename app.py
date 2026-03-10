import json
import os
import platform
import queue
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SERVERS_DIR = DATA_DIR / "servers"
DB_PATH = DATA_DIR / "manager.db"
APP_CONFIG = DATA_DIR / "config.json"
DEFAULT_SERVER = "main"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("MM_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


class ServerRuntime:
    def __init__(self):
        self.process = None
        self.thread = None
        self.status = "stopped"
        self.log_buffer = []
        self.console_clients = set()
        self.lock = threading.Lock()

    def append_log(self, line: str):
        with self.lock:
            self.log_buffer.append(line)
            self.log_buffer = self.log_buffer[-3000:]


RUNTIME = {DEFAULT_SERVER: ServerRuntime()}


# ----------------------------
# Database / auth helpers
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    if not APP_CONFIG.exists():
        APP_CONFIG.write_text(json.dumps({"java_path": "java", "memory": "2G"}, indent=2))


def get_setting(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def is_configured():
    return get_setting("admin_password_hash") is not None


def require_login():
    return session.get("authenticated") is True


def login_required(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not is_configured():
            return redirect(url_for("setup"))
        if not require_login():
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


# ----------------------------
# Files / server helpers
# ----------------------------
def server_path(name=DEFAULT_SERVER):
    return SERVERS_DIR / name


def load_app_config():
    return json.loads(APP_CONFIG.read_text())


def save_app_config(data):
    APP_CONFIG.write_text(json.dumps(data, indent=2))


def server_state(name=DEFAULT_SERVER):
    p = server_path(name)
    props = p / "server.properties"
    return {
        "name": name,
        "path": str(p),
        "exists": p.exists(),
        "status": RUNTIME[name].status,
        "jar": detect_server_jar(p),
        "eula": (p / "eula.txt").exists(),
        "properties": props.exists(),
    }


def detect_server_jar(path: Path):
    if not path.exists():
        return None
    jars = sorted(path.glob("*.jar"), key=lambda x: x.stat().st_mtime, reverse=True)
    return jars[0].name if jars else None


def read_properties(path: Path):
    props = {}
    if not path.exists():
        return props
    for line in path.read_text(errors="ignore").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def write_properties(path: Path, props: dict):
    lines = [f"{k}={v}" for k, v in props.items()]
    path.write_text("\n".join(lines) + "\n")


def allowed_plugin_url(url: str):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def log_file_path(name=DEFAULT_SERVER):
    p = server_path(name) / "logs" / "latest.log"
    return p


def latest_log(name=DEFAULT_SERVER, limit=300):
    p = log_file_path(name)
    if p.exists():
        lines = p.read_text(errors="ignore").splitlines()
        return "\n".join(lines[-limit:])
    return ""


# ----------------------------
# Installers
# ----------------------------
def install_server(server_type: str, mc_version: str, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    (target / "eula.txt").write_text("eula=true\n")

    if server_type == "paper":
        jar_name, url = paper_download_info(mc_version)
        download_file(url, target / jar_name)
        return f"Downloaded {jar_name}"

    if server_type == "fabric":
        jar_name, url = fabric_download_info(mc_version)
        download_file(url, target / jar_name)
        return f"Downloaded {jar_name}"

    if server_type == "spigot":
        buildtools = target / "BuildTools.jar"
        download_file("https://www.spigotmc.org/go/buildtools-dl", buildtools)
        return (
            "Downloaded BuildTools.jar. Run the provided build command from the UI after ensuring Java and Git are installed."
        )

    raise ValueError("Unsupported server type")


def paper_download_info(mc_version: str):
    query = {
        "query": """
        query($version: String!) {
          version(projectKey: \"paper\", versionKey: $version) {
            builds(filterBy: {channel: STABLE}, first: 1, orderBy: {direction: DESC}) {
              edges {
                node {
                  number
                  download(key: \"server:default\") {
                    name
                    url
                  }
                }
              }
            }
          }
        }
        """,
        "variables": {"version": mc_version},
    }
    r = requests.post("https://fill.papermc.io/graphql", json=query, timeout=30)
    r.raise_for_status()
    data = r.json()
    edges = data.get("data", {}).get("version", {}).get("builds", {}).get("edges", [])
    if not edges:
        raise RuntimeError(f"No Paper build found for {mc_version}")
    dl = edges[0]["node"]["download"]
    return dl["name"], dl["url"]


def fabric_download_info(mc_version: str):
    inst = requests.get("https://meta.fabricmc.net/v2/versions/installer", timeout=30)
    inst.raise_for_status()
    installer = inst.json()[0]["version"]
    loader_resp = requests.get("https://meta.fabricmc.net/v2/versions/loader", timeout=30)
    loader_resp.raise_for_status()
    loader = loader_resp.json()[0]["version"]
    url = f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}/{loader}/{installer}/server/jar"
    return f"fabric-server-mc.{mc_version}.jar", url


def download_file(url: str, target: Path):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


# ----------------------------
# Runtime
# ----------------------------
def start_server(name=DEFAULT_SERVER):
    runtime = RUNTIME[name]
    sdir = server_path(name)
    cfg = load_app_config()
    jar = detect_server_jar(sdir)
    if not jar:
        raise RuntimeError("No server jar found")
    if runtime.process and runtime.process.poll() is None:
        raise RuntimeError("Server is already running")

    java = cfg.get("java_path", "java")
    memory = cfg.get("memory", "2G")
    cmd = [java, f"-Xms{memory}", f"-Xmx{memory}", "-jar", jar, "nogui"]

    runtime.process = subprocess.Popen(
        cmd,
        cwd=sdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    runtime.status = "running"

    def pump():
        runtime.append_log(f"[manager] Started: {' '.join(cmd)}")
        socketio.emit("console_line", {"line": f"[manager] Started: {' '.join(cmd)}"}, to=f"console-{name}")
        for line in iter(runtime.process.stdout.readline, ""):
            line = line.rstrip("\n")
            runtime.append_log(line)
            socketio.emit("console_line", {"line": line}, to=f"console-{name}")
        rc = runtime.process.wait()
        runtime.status = "stopped"
        msg = f"[manager] Server exited with code {rc}"
        runtime.append_log(msg)
        socketio.emit("console_line", {"line": msg}, to=f"console-{name}")

    runtime.thread = threading.Thread(target=pump, daemon=True)
    runtime.thread.start()


def stop_server(name=DEFAULT_SERVER):
    runtime = RUNTIME[name]
    if not runtime.process or runtime.process.poll() is not None:
        raise RuntimeError("Server is not running")
    send_console_command("stop", name)


def send_console_command(command: str, name=DEFAULT_SERVER):
    runtime = RUNTIME[name]
    if not runtime.process or runtime.process.poll() is not None:
        raise RuntimeError("Server is not running")
    runtime.process.stdin.write(command + "\n")
    runtime.process.stdin.flush()


# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    if not is_configured():
        return redirect(url_for("setup"))
    if not require_login():
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if is_configured():
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 12:
            flash("Use at least 12 characters for the admin password.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        else:
            set_setting("admin_password_hash", generate_password_hash(password))
            flash("Admin account created. Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not is_configured():
        return redirect(url_for("setup"))
    if request.method == "POST":
        password = request.form.get("password", "")
        stored = get_setting("admin_password_hash")
        if stored and check_password_hash(stored, password):
            session.clear()
            session["authenticated"] = True
            session["login_at"] = time.time()
            return redirect(url_for("dashboard"))
        flash("Invalid password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    sdir = server_path()
    props = read_properties(sdir / "server.properties")
    plugin_dir = sdir / "plugins"
    mod_dir = sdir / "mods"
    worlds = [p.name for p in sdir.iterdir() if p.is_dir() and p.name in {"world", "world_nether", "world_the_end"}] if sdir.exists() else []
    plugins = sorted([p.name for p in plugin_dir.glob("*.jar")]) if plugin_dir.exists() else []
    mods = sorted([p.name for p in mod_dir.glob("*.jar")]) if mod_dir.exists() else []
    app_cfg = load_app_config()
    return render_template(
        "dashboard.html",
        state=server_state(),
        props=props,
        plugins=plugins,
        mods=mods,
        worlds=worlds,
        app_cfg=app_cfg,
        platform_name=platform.system(),
    )


@app.post("/install")
@login_required
def install():
    server_type = request.form.get("server_type", "paper").lower()
    version = request.form.get("mc_version", "1.21.1")
    target = server_path()
    try:
        msg = install_server(server_type, version, target)
        flash(msg, "success")
    except Exception as e:
        flash(f"Install failed: {e}", "danger")
    return redirect(url_for("dashboard"))


@app.post("/build_spigot")
@login_required
def build_spigot():
    sdir = server_path()
    buildtools = sdir / "BuildTools.jar"
    version = request.form.get("mc_version", "latest")
    if not buildtools.exists():
        flash("BuildTools.jar not found. Install Spigot first.", "danger")
        return redirect(url_for("dashboard"))
    cmd = [load_app_config().get("java_path", "java"), "-jar", "BuildTools.jar", "--rev", version]
    try:
        proc = subprocess.Popen(cmd, cwd=sdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = []
        for line in proc.stdout:
            output.append(line)
            RUNTIME[DEFAULT_SERVER].append_log(line.rstrip())
        proc.wait(timeout=3600)
        flash("Spigot build finished. Check logs/console section.", "success")
    except Exception as e:
        flash(f"Spigot build failed: {e}", "danger")
    return redirect(url_for("dashboard"))


@app.post("/server/start")
@login_required
def server_start():
    try:
        start_server()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/server/stop")
@login_required
def server_stop():
    try:
        stop_server()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/server/command")
@login_required
def server_command():
    data = request.get_json(force=True)
    try:
        send_console_command(data.get("command", ""))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.get("/server/status")
@login_required
def server_status():
    return jsonify(server_state())


@app.post("/properties/save")
@login_required
def save_properties_route():
    sdir = server_path()
    props_path = sdir / "server.properties"
    props = {}
    for key, value in request.form.items():
        if key.startswith("prop_"):
            props[key[5:]] = value
    write_properties(props_path, props)
    flash("server.properties saved.", "success")
    return redirect(url_for("dashboard"))


@app.post("/app_config/save")
@login_required
def save_app_config_route():
    cfg = load_app_config()
    cfg["java_path"] = request.form.get("java_path", "java")
    cfg["memory"] = request.form.get("memory", "2G")
    save_app_config(cfg)
    flash("Application config saved.", "success")
    return redirect(url_for("dashboard"))


@app.post("/plugin/upload")
@login_required
def plugin_upload():
    sdir = server_path()
    plugin_dir = sdir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    f = request.files.get("plugin_file")
    if not f or not f.filename.endswith(".jar"):
        flash("Upload a .jar plugin file.", "danger")
        return redirect(url_for("dashboard"))
    filename = secure_filename(f.filename)
    f.save(plugin_dir / filename)
    flash(f"Plugin uploaded: {filename}", "success")
    return redirect(url_for("dashboard"))


@app.post("/plugin/url")
@login_required
def plugin_url():
    sdir = server_path()
    plugin_dir = sdir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    url = request.form.get("plugin_url", "")
    if not allowed_plugin_url(url):
        flash("Invalid plugin URL.", "danger")
        return redirect(url_for("dashboard"))
    filename = secure_filename(Path(urlparse(url).path).name) or "plugin.jar"
    if not filename.endswith(".jar"):
        filename += ".jar"
    try:
        download_file(url, plugin_dir / filename)
        flash(f"Plugin downloaded: {filename}", "success")
    except Exception as e:
        flash(f"Plugin download failed: {e}", "danger")
    return redirect(url_for("dashboard"))


@app.post("/plugin/delete/<name>")
@login_required
def plugin_delete(name):
    target = server_path() / "plugins" / secure_filename(name)
    if target.exists():
        target.unlink()
        flash(f"Deleted {target.name}", "success")
    else:
        flash("Plugin not found.", "danger")
    return redirect(url_for("dashboard"))


@app.get("/logs")
@login_required
def logs():
    return jsonify({"log": latest_log() or "\n".join(RUNTIME[DEFAULT_SERVER].log_buffer[-300:])})


@app.post("/world/backup")
@login_required
def world_backup():
    sdir = server_path()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = sdir / f"world-backup-{timestamp}.zip"
    world_folders = [p for p in sdir.iterdir() if p.is_dir() and p.name.startswith("world")]
    if not world_folders:
        flash("No world folders found.", "danger")
        return redirect(url_for("dashboard"))
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in world_folders:
            for root, _, files in os.walk(folder):
                for file in files:
                    full = Path(root) / file
                    zf.write(full, full.relative_to(sdir))
    return send_file(backup_path, as_attachment=True)


@app.post("/world/upload")
@login_required
def world_upload():
    sdir = server_path()
    f = request.files.get("world_zip")
    if not f or not f.filename.endswith(".zip"):
        flash("Upload a zip file containing a world folder.", "danger")
        return redirect(url_for("dashboard"))
    tmp = sdir / "world_upload.zip"
    f.save(tmp)
    with zipfile.ZipFile(tmp, "r") as zf:
        zf.extractall(sdir)
    tmp.unlink(missing_ok=True)
    flash("World archive extracted.", "success")
    return redirect(url_for("dashboard"))


@app.post("/world/delete/<name>")
@login_required
def world_delete(name):
    target = server_path() / secure_filename(name)
    if target.exists() and target.is_dir() and target.name.startswith("world"):
        shutil.rmtree(target)
        flash(f"Deleted world folder {target.name}", "success")
    else:
        flash("World folder not found.", "danger")
    return redirect(url_for("dashboard"))


# ----------------------------
# Socket.IO
# ----------------------------
@socketio.on("join_console")
def handle_join(data):
    name = data.get("server", DEFAULT_SERVER)
    from flask_socketio import join_room

    join_room(f"console-{name}")
    history = RUNTIME[name].log_buffer[-200:]
    emit("console_history", {"lines": history})


# ----------------------------
# Main
# ----------------------------
init_db()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
