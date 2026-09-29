import atexit
import json
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Зависимости ставятся через nix develop (flake.nix). Windows-хак с
# подкладыванием env/Lib/site-packages в sys.path удалён: на macOS-venv путь
# не сходится, а голый ImportError с именем пакета — уже диагностика.
from selenium.webdriver import ChromeOptions

from config_reader import config
from engine.log import get_logger


log = get_logger()


def get_proxies() -> list[str]:
    """Get proxies from file

    :rtype: list
    :returns: List of proxies
    """

    filepath = Path(config.paths.proxy_file)

    if not filepath.exists():
        raise SystemExit(f"Couldn't find proxy file: {filepath}")

    with open(filepath, encoding="utf-8") as proxyfile:
        proxies = [
            proxy.strip().replace("'", "").replace('"', "")
            for proxy in proxyfile.read().splitlines()
        ]

    # blank lines would become empty proxies
    return [proxy for proxy in proxies if proxy]


# Endpoints that are still waiting for their extension to read the credentials.
_credentials_services: list[HTTPServer] = []


class _CredentialsHandler(BaseHTTPRequestHandler):
    """Hand the proxy credentials to the extension and shut the endpoint down

    The extension takes the credentials once, when its service worker starts,
    and keeps them in ``chrome.storage.session``, which lives in the browser
    memory only. After the first response the endpoint is closed, so the
    credentials cannot be read a second time.
    """

    def do_GET(self) -> None:
        credentials = self.server.credentials

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(credentials)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(credentials)

        # shutdown() must not be called from the serve_forever() thread,
        # otherwise it waits for itself; the socket is closed right after the
        # loop is stopped, so the endpoint refuses connections from now on.
        threading.Thread(
            target=_stop_credentials_service, args=(self.server,), daemon=True
        ).start()

    def log_message(self, message_format: str, *args) -> None:
        """Log the request line through the project logger instead of stderr"""

        log.debug(
            "proxy",
            "Proxy credentials endpoint",
            fields={"request": message_format % args},
        )


def _stop_credentials_service(server: HTTPServer) -> None:
    """Stop serving credentials and close the listening socket

    :type server: HTTPServer
    :param server: Credentials endpoint to close
    """

    server.shutdown()
    server.server_close()

    if server in _credentials_services:
        _credentials_services.remove(server)


def _open_credentials_service(username: str, password: str) -> int:
    """Serve the proxy credentials from memory instead of writing them to disk

    :type username: str
    :param username: Proxy username
    :type password: str
    :param password: Proxy password
    :rtype: int
    :returns: Port of the loopback endpoint that hands the credentials over
    """

    server = HTTPServer(("127.0.0.1", 0), _CredentialsHandler)
    server.credentials = json.dumps({"username": username, "password": password}).encode("utf-8")
    _credentials_services.append(server)

    threading.Thread(target=server.serve_forever, daemon=True).start()

    log.debug(
        "proxy",
        "Proxy credentials are served once",
        fields={"endpoint": f"127.0.0.1:{server.server_address[1]}"},
    )

    return server.server_address[1]


def _close_credentials_services() -> None:
    """Close every credentials endpoint that no extension has used yet"""

    for server in list(_credentials_services):
        _stop_credentials_service(server)


atexit.register(_close_credentials_services)


# Каталоги расширений, созданные в системном tempdir: их можно безопасно
# чистить при выходе. Явно переданный plugins_dir сюда не попадает и никогда
# не удаляется автоматически — там могут лежать чужие файлы.
_auto_plugin_dirs: list[Path] = []


def _plugins_base_dir(plugins_dir: str | Path | None = None) -> Path:
    """Каталог под расширения: явный или свежий в системном tempdir.

    Расширения больше не создаются в cwd: папка proxy_auth_plugin в рабочем
    каталоге переживала перезапуски и светилась в бэкапах. Chrome читает
    расширение с диска всё время работы, поэтому автосозданный tempdir живёт
    до выхода из процесса и чистится в _cleanup_auto_plugin_dirs.
    """

    if plugins_dir is not None:
        return Path(plugins_dir)

    created = Path(tempfile.mkdtemp(prefix="proxy_auth_plugin_"))
    _auto_plugin_dirs.append(created)
    return created


def _cleanup_auto_plugin_dirs() -> None:
    """Удалить автосозданные tempdir'ы. Явно переданные каталоги не трогает."""

    while _auto_plugin_dirs:
        shutil.rmtree(_auto_plugin_dirs.pop(), ignore_errors=True)


atexit.register(_cleanup_auto_plugin_dirs)


def install_plugin(
    chrome_options: ChromeOptions,
    proxy_host: str,
    proxy_port: int,
    username: str,
    password: str,
    plugin_folder_name: str,
    plugins_dir: str | Path | None = None,
) -> None:
    """Install plugin on the fly for proxy authentication

    The extension files only get the proxy host and port: the username and the
    password are handed over from memory through a one-shot loopback endpoint,
    so they never end up in the extension directory on disk.

    :type chrome_options: ChromeOptions
    :param chrome_options: ChromeOptions instance to add plugin
    :type proxy_host: str
    :param proxy_host: Proxy host
    :type proxy_port: int
    :param proxy_port: Proxy port
    :type username: str
    :param username: Proxy username
    :type password: str
    :param password: Proxy password
    :type plugin_folder_name: str
    :param plugin_folder_name: Plugin folder name for proxy
    :type plugins_dir: str | Path | None
    :param plugins_dir: Base directory for extensions. When omitted, a fresh
        directory under the system tempdir is used instead of cwd, so repeated
        runs stop polluting the working directory.
    """

    manifest_json = """
{
    "version": "1.0.0",
    "manifest_version": 3,
    "name": "Chrome Proxy Authentication",
    "background": {
        "service_worker": "background.js"
    },
    "permissions": [
        "proxy",
        "tabs",
        "unlimitedStorage",
        "storage",
        "webRequest",
        "webRequestAuthProvider"
    ],
    "host_permissions": [
        "<all_urls>"
    ],
    "minimum_chrome_version": "120"
}
"""

    background_js = """
var config = {
    mode: "fixed_servers",
    rules: {
        singleProxy: {
            scheme: "http",
            host: "%s",
            port: %s
        },
        bypassList: ["localhost"]
    }
};
chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

// The credentials are never written into the extension: install_plugin()
// serves them once from a loopback endpoint in the parent process, this worker
// takes them at startup and keeps them in chrome.storage.session, which lives
// in the browser memory. The endpoint is closed right after that single read.
var credentialsEndpoint = "http://127.0.0.1:%s/credentials";
var credentials = null;

function loadCredentials() {
    if (credentials) {
        return Promise.resolve(credentials);
    }

    return chrome.storage.session.get("proxyCredentials").then(function (stored) {
        if (stored && stored.proxyCredentials) {
            credentials = stored.proxyCredentials;
            return credentials;
        }

        return fetch(credentialsEndpoint).then(function (response) {
            return response.json();
        }).then(function (loaded) {
            credentials = loaded;
            return chrome.storage.session.set({ proxyCredentials: loaded }).then(function () {
                return credentials;
            });
        });
    });
}

loadCredentials();

chrome.webRequest.onAuthRequired.addListener(
    function (details, callback) {
        loadCredentials().then(function (loaded) {
            callback({ authCredentials: loaded });
        }, function () {
            callback({});
        });

        return true;
    },
    { urls: ["<all_urls>"] },
    ['asyncBlocking']
);
""" % (
        proxy_host,
        proxy_port,
        _open_credentials_service(username, password),
    )

    plugins_folder = _plugins_base_dir(plugins_dir)
    plugins_folder.mkdir(parents=True, exist_ok=True)

    plugin_folder = plugins_folder / plugin_folder_name

    log.debug("proxy", "Creating folder...", fields={"folder": str(plugin_folder)})
    plugin_folder.mkdir(exist_ok=True)

    manifest_path = plugin_folder / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as manifest_file:
        manifest_file.write(manifest_json)

    background_path = plugin_folder / "background.js"
    with open(background_path, "w", encoding="utf-8") as background_js_file:
        background_js_file.write(background_js)

    if not manifest_path.exists() or not background_path.exists():
        raise RuntimeError("Failed to create extension files")

    extension_path = str(plugin_folder.resolve())
    log.debug("proxy", "Loading extension from", fields={"path": extension_path})

    chrome_options.add_argument(f"--load-extension={extension_path}")
