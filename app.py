import asyncio
import base64
import contextlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path(os.environ.get("PYMIUM_TEMP_DIR", str(BASE_DIR / ".pymium-tmp"))).resolve()
CACHE_DIR = Path(os.environ.get("PYMIUM_CACHE_DIR", str(BASE_DIR / ".pymium-cache"))).resolve()
LOG_DIR = Path(os.environ.get("PYMIUM_LOG_DIR", str(BASE_DIR / "logs"))).resolve()
LOG_FILE = LOG_DIR / "pymium.log"
LOCAL_LIBS_DIR = Path(os.environ.get("PYMIUM_LOCAL_LIB_DIR", str(BASE_DIR / ".pymium-system-libs"))).resolve()
PLAYWRIGHT_BROWSERS_DIR = Path(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / ".playwright-browsers"))
).resolve()

for directory in (TEMP_DIR, CACHE_DIR, LOG_DIR, LOCAL_LIBS_DIR, PLAYWRIGHT_BROWSERS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_DIR)
for env_key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
    os.environ[env_key] = str(TEMP_DIR)
os.environ.setdefault("PIP_CACHE_DIR", str((CACHE_DIR / "pip").resolve()))
os.environ.setdefault("XDG_CACHE_HOME", str((CACHE_DIR / "xdg").resolve()))
Path(os.environ["PIP_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEMP_DIR)


def configure_logging() -> logging.Logger:
    level_name = os.environ.get("PYMIUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    existing_names = {handler.get_name() for handler in root_logger.handlers}

    handlers: list[tuple[str, logging.Handler]] = [
        ("pymium-stream", logging.StreamHandler(sys.stdout)),
        (
            "pymium-file",
            RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        ),
    ]

    for name, handler in handlers:
        if name in existing_names:
            continue
        handler.set_name(name)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    for logger_name in ("hypercorn.error", "hypercorn.access", "quart.app"):
        named_logger = logging.getLogger(logger_name)
        named_logger.handlers.clear()
        named_logger.propagate = True
        named_logger.setLevel(level)

    logging.captureWarnings(True)
    logger = logging.getLogger("pymium")
    logger.setLevel(level)
    logger.info("Logging to %s", LOG_FILE)
    logger.info("Temporary directory set to %s", TEMP_DIR)
    logger.info("Playwright browsers path set to %s", PLAYWRIGHT_BROWSERS_DIR)
    return logger


LOGGER = configure_logging()
MISSING_SHARED_LIBRARY_RE = re.compile(
    r"error while loading shared libraries: ([^:\n]+): cannot open shared object file"
)

SHARED_LIBRARY_HINTS = {
    "libnspr4.so": [
        "Debian/Ubuntu 系: apt-get update && apt-get install -y libnspr4 libnss3",
        "Alpine 系: apk add --no-cache nspr nss",
    ],
    "libnss3.so": [
        "Debian/Ubuntu 系: apt-get update && apt-get install -y libnss3 libnspr4",
        "Alpine 系: apk add --no-cache nss nspr",
    ],
}

SYSTEM_PACKAGE_MAP = {
    "apt-get": {
        "libnspr4.so": ["libnspr4", "libnss3"],
        "libnss3.so": ["libnss3", "libnspr4"],
        "libatk-1.0.so.0": ["libatk1.0-0"],
        "libatk-bridge-2.0.so.0": ["libatk-bridge2.0-0"],
        "libatspi.so.0": ["libatspi2.0-0"],
        "libgtk-3.so.0": ["libgtk-3-0"],
        "libx11-xcb.so.1": ["libx11-xcb1"],
        "libxcomposite.so.1": ["libxcomposite1"],
        "libxdamage.so.1": ["libxdamage1"],
        "libxfixes.so.3": ["libxfixes3"],
        "libxrandr.so.2": ["libxrandr2"],
        "libxkbcommon.so.0": ["libxkbcommon0"],
        "libgbm.so.1": ["libgbm1"],
        "libdrm.so.2": ["libdrm2"],
        "libasound.so.2": ["libasound2"],
        "libcups.so.2": ["libcups2"],
        "libpango-1.0.so.0": ["libpango-1.0-0"],
        "libpangocairo-1.0.so.0": ["libpangocairo-1.0-0"],
        "libcairo.so.2": ["libcairo2"],
    },
    "apk": {
        "libnspr4.so": ["nspr", "nss"],
        "libnss3.so": ["nss", "nspr"],
        "libatk-1.0.so.0": ["atk"],
        "libatk-bridge-2.0.so.0": ["at-spi2-core"],
        "libatspi.so.0": ["at-spi2-core"],
        "libgtk-3.so.0": ["gtk+3.0"],
        "libx11-xcb.so.1": ["libx11"],
        "libxcomposite.so.1": ["libxcomposite"],
        "libxdamage.so.1": ["libxdamage"],
        "libxfixes.so.3": ["libxfixes"],
        "libxrandr.so.2": ["libxrandr"],
        "libxkbcommon.so.0": ["libxkbcommon"],
        "libgbm.so.1": ["mesa-gbm"],
        "libdrm.so.2": ["mesa-dri-gallium"],
        "libasound.so.2": ["alsa-lib"],
        "libcups.so.2": ["cups-libs"],
        "libpango-1.0.so.0": ["pango"],
        "libpangocairo-1.0.so.0": ["pango"],
        "libcairo.so.2": ["cairo"],
    },
    "dnf": {
        "libnspr4.so": ["nspr", "nss"],
        "libnss3.so": ["nss", "nspr"],
    },
    "yum": {
        "libnspr4.so": ["nspr", "nss"],
        "libnss3.so": ["nss", "nspr"],
    },
}

RUNTIME_REQUIREMENTS = {
    "quart": "quart>=0.19,<1.0",
    "playwright": "playwright>=1.52,<2.0",
}


def run_logged_subprocess(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    log_prefix: str = "subprocess",
) -> str:
    LOGGER.info("Running %s command: %s", log_prefix, " ".join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="ignore",
        bufsize=1,
    )

    tail = deque(maxlen=200)
    assert process.stdout is not None
    for line in process.stdout:
        tail.append(line)
        stripped = line.rstrip()
        if stripped:
            LOGGER.info("[%s] %s", log_prefix, stripped)

    return_code = process.wait()
    tail_text = "".join(tail)
    if return_code != 0:
        raise RuntimeError(
            f"{log_prefix} command failed with exit code {return_code}: {' '.join(command)}\n\n{tail_text}"
        )

    return tail_text


def playwright_runtime_present() -> bool:
    if not PLAYWRIGHT_BROWSERS_DIR.exists():
        return False

    for child in PLAYWRIGHT_BROWSERS_DIR.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(("chromium-", "chromium_headless_shell-")):
            return True

    return False


def detect_package_manager() -> Optional[str]:
    for command in ("apt-get", "apk", "dnf", "yum"):
        if shutil.which(command):
            return command
    return None


async def run_logged_async_subprocess(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    log_prefix: str = "subprocess",
) -> str:
    LOGGER.info("Running %s command: %s", log_prefix, " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    output: list[str] = []
    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore")
        output.append(decoded)
        stripped = decoded.rstrip()
        if stripped:
            LOGGER.info("[%s] %s", log_prefix, stripped)

    return_code = await process.wait()
    tail_text = "".join(output[-200:])
    if return_code != 0:
        raise RuntimeError(
            f"{log_prefix} command failed with exit code {return_code}: {' '.join(command)}\n\n{tail_text}"
        )

    return tail_text


def diagnose_runtime_error(error_text: str) -> tuple[str, Optional[str]]:
    match = MISSING_SHARED_LIBRARY_RE.search(error_text)
    if not match:
        return error_text, None

    library_name = match.group(1)
    package_manager = detect_package_manager()
    package_candidates = SYSTEM_PACKAGE_MAP.get(package_manager or "", {}).get(library_name, [])
    hints = SHARED_LIBRARY_HINTS.get(
        library_name,
        [
            f"Debian/Ubuntu 系: apt-get update && apt-get install -y {library_name}",
            f"Alpine 系: apk add --no-cache <{library_name} を含むパッケージ>",
        ],
    )
    hint_text = "\n".join(f"- {hint}" for hint in hints)
    auto_install_text = (
        f"Pymium は Python 側から {package_manager} で自動導入を試みます: {' '.join(package_candidates)}"
        if package_manager and package_candidates
        else "Pymium は Python 側から自動導入を試みられる場合がありますが、対応するパッケージマネージャを判定できませんでした。"
    )
    rootless_text = (
        "さらに Debian/Ubuntu 系で apt が使える場合は、root なしでも apt download + dpkg-deb -x でローカル共有ライブラリ展開を試みます。"
        if package_manager == "apt-get"
        else ""
    )
    message = (
        f"Chromium の起動に必要な共有ライブラリが不足しています: {library_name}\n"
        f"{auto_install_text}\n"
        "ただし自動導入には root 権限と OS パッケージマネージャが必要です。\n"
        f"{rootless_text}\n"
        "例:\n"
        f"{hint_text}\n\n"
        "---- 元のエラー ----\n"
        f"{error_text}"
    )
    return message, library_name


def ensure_runtime_dependencies() -> None:
    missing: list[str] = []
    for module_name, package_spec in RUNTIME_REQUIREMENTS.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(package_spec)

    if not missing:
        return

    LOGGER.info("Installing missing Python packages: %s", ", ".join(missing))
    run_logged_subprocess(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *missing,
        ],
        cwd=BASE_DIR,
        env=os.environ.copy(),
        log_prefix="pip",
    )
    importlib.invalidate_caches()


ensure_runtime_dependencies()

from quart import Quart, jsonify, render_template_string, request, websocket
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


APP_TITLE = "Pymium Remote Chromium"
DEFAULT_PORT = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "8000")))
DEFAULT_URL = os.environ.get("START_URL", "about:blank")
DEFAULT_WIDTH = int(os.environ.get("BROWSER_WIDTH", "1600"))
DEFAULT_HEIGHT = int(os.environ.get("BROWSER_HEIGHT", "900"))
ACTIVE_WINDOW_SECONDS = float(os.environ.get("ACTIVE_WINDOW_SECONDS", "1.4"))
ACTIVE_FPS = float(os.environ.get("ACTIVE_FPS", "24"))
IDLE_FPS = float(os.environ.get("IDLE_FPS", "5"))
ACTIVE_JPEG_QUALITY = int(os.environ.get("ACTIVE_JPEG_QUALITY", "80"))
IDLE_JPEG_QUALITY = int(os.environ.get("IDLE_JPEG_QUALITY", "62"))
AUTO_INSTALL_SYSTEM_DEPS = os.environ.get("PYMIUM_AUTO_INSTALL_SYSTEM_DEPS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
IS_LINUX = sys.platform.startswith("linux")
DEBIAN_PACKAGE_SUITE = os.environ.get("PYMIUM_DEBIAN_SUITE", "bookworm")
DEBIAN_PACKAGE_ARCH = os.environ.get("PYMIUM_DEBIAN_ARCH", "amd64")


def discover_shared_library_dirs(base_dir: Path) -> list[str]:
    if not base_dir.exists():
        return []

    directories: list[str] = []
    for root, _, files in os.walk(base_dir):
        if any(".so" in file_name for file_name in files):
            root_path = str(Path(root).resolve())
            if root_path not in directories:
                directories.append(root_path)
    return directories


def prepend_library_dirs_to_env(directories: list[str]) -> None:
    if not directories:
        return

    existing = [item for item in os.environ.get("LD_LIBRARY_PATH", "").split(":") if item]
    merged: list[str] = []
    for path in directories + existing:
        if path and path not in merged:
            merged.append(path)
    os.environ["LD_LIBRARY_PATH"] = ":".join(merged)


prepend_library_dirs_to_env(discover_shared_library_dirs(LOCAL_LIBS_DIR))


def library_exists_in_local_bundle(library_name: str) -> bool:
    for root, _, files in os.walk(LOCAL_LIBS_DIR):
        if library_name in files:
            return True
    return False


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        return "about:blank"

    lowered = url.lower()
    if lowered.startswith(("http://", "https://", "about:", "data:")):
        return url

    return f"https://{url}"


def button_name(button: int) -> str:
    return {1: "middle", 2: "right"}.get(button, "left")


def playwright_key_from_code(code: Optional[str], key: Optional[str]) -> Optional[str]:
    code = code or ""
    key = key or ""

    named = {
        "Backspace": "Backspace",
        "Tab": "Tab",
        "Enter": "Enter",
        "ShiftLeft": "Shift",
        "ShiftRight": "Shift",
        "ControlLeft": "Control",
        "ControlRight": "Control",
        "AltLeft": "Alt",
        "AltRight": "Alt",
        "Pause": "Pause",
        "CapsLock": "CapsLock",
        "Escape": "Escape",
        "Space": "Space",
        "PageUp": "PageUp",
        "PageDown": "PageDown",
        "End": "End",
        "Home": "Home",
        "ArrowLeft": "ArrowLeft",
        "ArrowUp": "ArrowUp",
        "ArrowRight": "ArrowRight",
        "ArrowDown": "ArrowDown",
        "Insert": "Insert",
        "Delete": "Delete",
        "MetaLeft": "Meta",
        "MetaRight": "Meta",
        "ContextMenu": "ContextMenu",
        "NumLock": "NumLock",
        "ScrollLock": "ScrollLock",
        "Minus": "-",
        "Equal": "=",
        "BracketLeft": "[",
        "BracketRight": "]",
        "Backslash": "\\",
        "Semicolon": ";",
        "Quote": "'",
        "Backquote": "`",
        "Comma": ",",
        "Period": ".",
        "Slash": "/",
        "NumpadAdd": "+",
        "NumpadSubtract": "-",
        "NumpadMultiply": "*",
        "NumpadDivide": "/",
        "NumpadDecimal": ".",
        "NumpadEnter": "Enter",
    }

    if code in named:
        return named[code]

    if code.startswith("Key") and len(code) == 4:
        return code[-1].lower()

    if code.startswith("Digit") and len(code) == 6:
        return code[-1]

    if code.startswith("Numpad") and len(code) == 7 and code[-1].isdigit():
        return code[-1]

    if key.startswith("F") and key[1:].isdigit():
        return key

    if len(key) == 1:
        return key.lower()

    return key or None


class FrameClient:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)

    def push(self, data: bytes) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(data)


class FrameHub:
    def __init__(self) -> None:
        self.clients: set[FrameClient] = set()
        self.latest_frame: Optional[bytes] = None

    def register(self) -> FrameClient:
        client = FrameClient()
        self.clients.add(client)
        if self.latest_frame is not None:
            client.push(self.latest_frame)
        return client

    def unregister(self, client: FrameClient) -> None:
        self.clients.discard(client)

    def publish(self, data: bytes) -> None:
        self.latest_frame = data
        for client in tuple(self.clients):
            client.push(data)


class BrowserManager:
    def __init__(self) -> None:
        self.frame_hub = FrameHub()
        self.start_lock = asyncio.Lock()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp_session = None
        self.capture_task: Optional[asyncio.Task[Any]] = None
        self.mode_task: Optional[asyncio.Task[Any]] = None
        self.start_task: Optional[asyncio.Task[Any]] = None
        self.state = "stopped"
        self.capture_backend = "none"
        self.viewport_width = DEFAULT_WIDTH
        self.viewport_height = DEFAULT_HEIGHT
        self.current_url = DEFAULT_URL
        self.last_error = ""
        self.warning = ""
        self.install_log = ""
        self.installing = False
        self.last_interaction_at = time.monotonic()
        self.screencast_mode = "idle"
        self.blocked_error = ""
        self.missing_shared_library: Optional[str] = None
        self.system_package_manager = detect_package_manager()
        self.auto_install_system_deps = AUTO_INSTALL_SYSTEM_DEPS
        self.system_dependency_attempted: set[str] = set()
        self.system_dependency_log = ""
        self.apt_updated = False
        self.blocked_warning_emitted = False
        self.playwright_deps_bootstrap_attempted = False

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "capture_backend": self.capture_backend,
            "installing": self.installing,
            "current_url": self.current_url,
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
            "error": self.last_error,
            "warning": self.warning,
            "install_log": self.install_log[-4000:],
            "browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
            "log_path": str(LOG_FILE),
            "temp_dir": str(TEMP_DIR),
            "cache_dir": str(CACHE_DIR),
            "local_lib_dir": str(LOCAL_LIBS_DIR),
            "blocked_error": self.blocked_error,
            "missing_shared_library": self.missing_shared_library,
            "system_package_manager": self.system_package_manager,
            "auto_install_system_deps": self.auto_install_system_deps,
            "system_dependency_log": self.system_dependency_log[-4000:],
        }

    def touch(self) -> None:
        self.last_interaction_at = time.monotonic()

    def _handle_frame_navigated(self, frame: Any) -> None:
        if self.page is not None and frame == self.page.main_frame:
            self.current_url = frame.url

    def _packages_for_library(self, library_name: str) -> list[str]:
        if not self.system_package_manager:
            return []
        return SYSTEM_PACKAGE_MAP.get(self.system_package_manager, {}).get(library_name, [])

    async def _install_system_packages(self, packages: list[str]) -> str:
        env = os.environ.copy()
        manager = self.system_package_manager
        assert manager is not None

        collected_output: list[str] = []
        if manager == "apt-get":
            if not self.apt_updated:
                collected_output.append(
                    await run_logged_async_subprocess(
                        ["apt-get", "update"],
                        env=env,
                        log_prefix="apt-update",
                    )
                )
                self.apt_updated = True
            collected_output.append(
                await run_logged_async_subprocess(
                    ["apt-get", "install", "-y", "--no-install-recommends", *packages],
                    env=env,
                    log_prefix="apt-install",
                )
            )
        elif manager == "apk":
            collected_output.append(
                await run_logged_async_subprocess(
                    ["apk", "add", "--no-cache", *packages],
                    env=env,
                    log_prefix="apk-add",
                )
            )
        elif manager == "dnf":
            collected_output.append(
                await run_logged_async_subprocess(
                    ["dnf", "install", "-y", *packages],
                    env=env,
                    log_prefix="dnf-install",
                )
            )
        elif manager == "yum":
            collected_output.append(
                await run_logged_async_subprocess(
                    ["yum", "install", "-y", *packages],
                    env=env,
                    log_prefix="yum-install",
                )
            )
        else:
            raise RuntimeError(f"Unsupported package manager: {manager}")

        return "\n".join(part for part in collected_output if part)

    async def _install_local_debian_packages(self, packages: list[str]) -> str:
        apt_command = shutil.which("apt")
        dpkg_deb = shutil.which("dpkg-deb")
        if not apt_command or not dpkg_deb:
            raise RuntimeError("root不要の Debian パッケージ展開に必要な apt と dpkg-deb が見つかりません。")

        download_dir = CACHE_DIR / "apt-downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"

        collected_output = [
            await run_logged_async_subprocess(
                [apt_command, "download", *packages],
                cwd=download_dir,
                env=env,
                log_prefix="apt-download",
            )
        ]

        for package in packages:
            candidates = sorted(download_dir.glob(f"{package}_*.deb"), key=lambda item: item.stat().st_mtime)
            if not candidates:
                raise RuntimeError(f"{package} の .deb ファイルを取得できませんでした。")

            deb_file = candidates[-1]
            collected_output.append(
                await run_logged_async_subprocess(
                    [dpkg_deb, "-x", str(deb_file), str(LOCAL_LIBS_DIR)],
                    env=env,
                    log_prefix="dpkg-deb",
                )
            )

        prepend_library_dirs_to_env(discover_shared_library_dirs(LOCAL_LIBS_DIR))
        return "\n".join(part for part in collected_output if part)

    async def _maybe_install_playwright_system_dependencies(self) -> None:
        if not IS_LINUX:
            return
        if not self.auto_install_system_deps:
            return
        if self.playwright_deps_bootstrap_attempted:
            return
        if self.system_package_manager != "apt-get":
            return
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            LOGGER.warning("Skipping playwright install-deps because root 権限がありません")
            return

        self.playwright_deps_bootstrap_attempted = True
        self.installing = True
        self.state = "installing-system-deps"
        try:
            LOGGER.info("Running playwright install-deps chromium")
            output = await run_logged_async_subprocess(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                cwd=BASE_DIR,
                env=os.environ.copy(),
                log_prefix="playwright-install-deps",
            )
            self.system_dependency_log = output[-12000:]
            self.install_log = self.system_dependency_log
        except Exception as exc:
            self.system_dependency_log = str(exc)[-12000:]
            self.install_log = self.system_dependency_log
            LOGGER.warning("playwright install-deps failed; will continue with targeted fallback", exc_info=exc)
        finally:
            self.installing = False

    async def _auto_install_system_dependency(self, library_name: str) -> tuple[bool, str]:
        if not self.auto_install_system_deps:
            message = "PYMIUM_AUTO_INSTALL_SYSTEM_DEPS=0 のため自動導入は無効です。"
            LOGGER.warning(message)
            return False, message

        if library_name in self.system_dependency_attempted:
            message = f"{library_name} の自動導入は既に試行済みです。"
            LOGGER.warning(message)
            return False, message

        if not self.system_package_manager:
            message = "対応する OS パッケージマネージャを検出できませんでした。"
            LOGGER.warning(message)
            return False, message

        packages = self._packages_for_library(library_name)
        if not packages:
            message = f"{library_name} に対応する自動導入パッケージが未定義です。"
            LOGGER.warning(message)
            return False, message

        if library_exists_in_local_bundle(library_name):
            prepend_library_dirs_to_env(discover_shared_library_dirs(LOCAL_LIBS_DIR))
            message = f"{library_name} は既にローカル共有ライブラリ束に存在するため、それを使って再試行します。"
            LOGGER.info(message)
            self.warning = message
            return True, message

        if hasattr(os, "geteuid") and os.geteuid() != 0:
            if self.system_package_manager == "apt-get":
                try:
                    LOGGER.info(
                        "Root 権限なしのため、Debian パッケージをローカル展開して %s を補います (%s)",
                        library_name,
                        " ".join(packages),
                    )
                    output = await self._install_local_debian_packages(packages)
                    self.system_dependency_log = output[-12000:]
                    self.install_log = self.system_dependency_log
                    self.warning = (
                        f"不足していた {library_name} に対して Debian パッケージをローカル展開しました。Chromium を再試行します。"
                    )
                    return True, self.warning
                except Exception as exc:
                    message = f"root不要のローカル共有ライブラリ展開に失敗しました: {exc}"
                    self.system_dependency_log = str(exc)[-12000:]
                    self.install_log = self.system_dependency_log
                    LOGGER.exception(message)
                    return False, message

            message = "OS パッケージの自動導入には root 権限が必要ですが、現在のプロセスは root ではありません。"
            LOGGER.warning(message)
            return False, message

        self.system_dependency_attempted.add(library_name)
        self.installing = True
        self.state = "installing-system-deps"
        try:
            LOGGER.info(
                "Attempting to auto-install missing shared library %s via %s (%s)",
                library_name,
                self.system_package_manager,
                " ".join(packages),
            )
            output = await self._install_system_packages(packages)
            self.system_dependency_log = output[-12000:]
            self.install_log = self.system_dependency_log
            self.warning = (
                f"不足していた {library_name} に対して OS パッケージを自動導入しました。Chromium を再試行します。"
            )
            LOGGER.info("System package install for %s completed", library_name)
            return True, self.warning
        except Exception as exc:
            message = f"{library_name} の自動導入に失敗しました: {exc}"
            self.system_dependency_log = str(exc)[-12000:]
            self.install_log = self.system_dependency_log
            self.warning = message
            LOGGER.exception(message)
            return False, message
        finally:
            self.installing = False

    async def install_browser_runtime(self) -> None:
        self.installing = True
        self.install_log = ""
        self.state = "installing"

        if playwright_runtime_present():
            LOGGER.info("Chromium runtime already present; skipping download")
            self.installing = False
            return

        LOGGER.info("Ensuring Chromium runtime is installed")

        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
        )

        output: list[str] = []
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="ignore")
            output.append(decoded)
            self.install_log = "".join(output)[-12000:]
            stripped = decoded.rstrip()
            if stripped:
                LOGGER.info("[playwright-install] %s", stripped)

        return_code = await process.wait()
        self.installing = False
        if return_code != 0:
            LOGGER.error("Chromium auto download failed with exit code %s", return_code)
            raise RuntimeError(
                "Chromium の自動ダウンロードに失敗しました。\n"
                f"command: {' '.join(cmd)}\n\n{self.install_log}"
            )
        LOGGER.info("Chromium runtime is ready")

    async def ensure_started(self, force: bool = False) -> None:
        async with self.start_lock:
            if self.page is not None and self.state in {"running", "installing", "starting"}:
                return

            if self.blocked_error and not force:
                if not self.blocked_warning_emitted:
                    LOGGER.warning("Startup is blocked until container dependencies are fixed")
                    self.blocked_warning_emitted = True
                self.state = "error"
                self.last_error = self.blocked_error
                return

            await self._cleanup(keep_error=False, next_state="starting")
            self.warning = ""
            self.last_error = ""
            self.blocked_error = ""
            self.blocked_warning_emitted = False
            self.missing_shared_library = None
            self.state = "starting"

            while True:
                try:
                    await self.install_browser_runtime()
                    await self._maybe_install_playwright_system_dependencies()

                    self.playwright = await async_playwright().start()
                    launch_args = [
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-background-networking",
                        "--disable-features=Translate,BackForwardCache",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--autoplay-policy=no-user-gesture-required",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--hide-scrollbars",
                        "--mute-audio",
                        "--password-store=basic",
                        "--use-mock-keychain",
                        "--disable-sync",
                        "--disable-breakpad",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                    ]

                    self.browser = await self.playwright.chromium.launch(
                        headless=True,
                        args=launch_args,
                        env=os.environ.copy(),
                    )
                    self.context = await self.browser.new_context(
                        viewport={
                            "width": self.viewport_width,
                            "height": self.viewport_height,
                        },
                        device_scale_factor=1,
                        ignore_https_errors=True,
                    )
                    self.page = await self.context.new_page()
                    self.page.on("framenavigated", self._handle_frame_navigated)
                    self.page.set_default_navigation_timeout(45000)
                    await self.page.goto(self.current_url, wait_until="domcontentloaded")
                    self.current_url = self.page.url
                    self.touch()
                    await self._start_capture()
                    self.state = "running"
                    LOGGER.info(
                        "Chromium started successfully at %s with viewport %sx%s",
                        self.current_url,
                        self.viewport_width,
                        self.viewport_height,
                    )
                    return
                except Exception as exc:
                    formatted_error = self._format_exception(exc)
                    diagnosed_error, missing_library = diagnose_runtime_error(formatted_error)
                    LOGGER.exception("Failed to start Chromium session")
                    await self._cleanup(keep_error=True, next_state="error")

                    self.last_error = diagnosed_error
                    self.missing_shared_library = missing_library
                    if missing_library:
                        installed, auto_message = await self._auto_install_system_dependency(missing_library)
                        if installed:
                            self.last_error = ""
                            self.blocked_error = ""
                            self.missing_shared_library = None
                            self.state = "starting"
                            continue

                        if auto_message:
                            self.last_error = f"{diagnosed_error}\n\n---- 自動導入結果 ----\n{auto_message}"
                        self.blocked_error = self.last_error
                        self.blocked_warning_emitted = False
                    return

    async def restart(self) -> None:
        LOGGER.info("Restarting Chromium session")
        self.system_dependency_attempted.clear()
        self.blocked_warning_emitted = False
        async with self.start_lock:
            await self._cleanup(keep_error=False, next_state="restarting")
        await self.ensure_started(force=True)

    async def stop(self) -> None:
        LOGGER.info("Stopping Chromium session")
        async with self.start_lock:
            await self._cleanup(keep_error=True, next_state="stopped")

    async def goto(self, raw_url: str) -> None:
        await self.ensure_started()
        if self.page is None:
            return

        target = normalize_url(raw_url)
        self.touch()
        LOGGER.info("Navigating to %s", target)
        await self.page.goto(target, wait_until="domcontentloaded")
        self.current_url = self.page.url

    async def reload(self) -> None:
        await self.ensure_started()
        if self.page is None:
            return
        self.touch()
        LOGGER.info("Reloading current page")
        await self.page.reload(wait_until="domcontentloaded")
        self.current_url = self.page.url

    async def back(self) -> None:
        await self.ensure_started()
        if self.page is None:
            return
        self.touch()
        LOGGER.info("Navigating back")
        response = await self.page.go_back(wait_until="domcontentloaded")
        if response is not None:
            self.current_url = self.page.url

    async def forward(self) -> None:
        await self.ensure_started()
        if self.page is None:
            return
        self.touch()
        LOGGER.info("Navigating forward")
        response = await self.page.go_forward(wait_until="domcontentloaded")
        if response is not None:
            self.current_url = self.page.url

    async def set_viewport(self, width: int, height: int) -> None:
        width = max(320, min(int(width), 4096))
        height = max(240, min(int(height), 4096))
        self.viewport_width = width
        self.viewport_height = height
        LOGGER.info("Viewport set to %sx%s", width, height)

        if self.page is None:
            return

        await self.page.set_viewport_size({"width": width, "height": height})
        if self.capture_backend == "cdp-screencast" and self.cdp_session is not None:
            await self._apply_screencast_profile(force=True)

    async def handle_client_event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")

        if event_type == "resize":
            await self.set_viewport(int(payload.get("width", DEFAULT_WIDTH)), int(payload.get("height", DEFAULT_HEIGHT)))
            return

        if event_type == "goto":
            await self.goto(str(payload.get("url", "about:blank")))
            return

        if event_type == "reload":
            await self.reload()
            return

        await self.ensure_started()
        if self.page is None:
            return

        self.touch()

        if event_type == "mouse_move":
            await self.page.mouse.move(float(payload.get("x", 0)), float(payload.get("y", 0)))
        elif event_type == "mouse_down":
            await self.page.mouse.move(float(payload.get("x", 0)), float(payload.get("y", 0)))
            await self.page.mouse.down(button=button_name(int(payload.get("button", 0))))
        elif event_type == "mouse_up":
            await self.page.mouse.move(float(payload.get("x", 0)), float(payload.get("y", 0)))
            await self.page.mouse.up(button=button_name(int(payload.get("button", 0))))
        elif event_type == "mouse_wheel":
            await self.page.mouse.wheel(float(payload.get("delta_x", 0)), float(payload.get("delta_y", 0)))
        elif event_type == "keydown":
            key_name = playwright_key_from_code(payload.get("code"), payload.get("key"))
            if key_name:
                await self.page.keyboard.down(key_name)
        elif event_type == "keyup":
            key_name = playwright_key_from_code(payload.get("code"), payload.get("key"))
            if key_name:
                await self.page.keyboard.up(key_name)
        elif event_type == "insert_text":
            text = str(payload.get("text", ""))
            if text:
                await self.page.keyboard.insert_text(text)

    async def _start_capture(self) -> None:
        if self.page is None or self.context is None:
            return

        self.capture_backend = "starting"
        self.screencast_mode = "idle"

        try:
            self.cdp_session = await self.context.new_cdp_session(self.page)
            self.cdp_session.on(
                "Page.screencastFrame",
                lambda params: asyncio.create_task(self._handle_screencast_frame(params)),
            )
            await self.cdp_session.send("Page.enable")
            await self._apply_screencast_profile(force=True)
            self.capture_backend = "cdp-screencast"
            self.mode_task = asyncio.create_task(self._screencast_mode_loop())
            return
        except Exception as exc:
            self.warning = (
                "CDP screencast を開始できなかったため screenshot fallback に切り替えました。\n\n"
                + self._format_exception(exc)
            )
            LOGGER.warning("CDP screencast unavailable; switching to screenshot fallback", exc_info=exc)
            self.capture_backend = "screenshot-fallback"
            self.cdp_session = None

        self.capture_task = asyncio.create_task(self._screenshot_loop())

    async def _handle_screencast_frame(self, params: dict[str, Any]) -> None:
        if self.cdp_session is None:
            return

        try:
            data = base64.b64decode(params.get("data", ""))
            if data:
                self.frame_hub.publish(data)
            await self.cdp_session.send(
                "Page.screencastFrameAck",
                {"sessionId": params["sessionId"]},
            )
        except Exception:
            pass

    async def _apply_screencast_profile(self, force: bool = False) -> None:
        if self.cdp_session is None:
            return

        mode = "active" if (time.monotonic() - self.last_interaction_at) < ACTIVE_WINDOW_SECONDS else "idle"
        if not force and mode == self.screencast_mode:
            return

        with contextlib.suppress(Exception):
            await self.cdp_session.send("Page.stopScreencast")

        quality = ACTIVE_JPEG_QUALITY if mode == "active" else IDLE_JPEG_QUALITY
        every_n = 1 if mode == "active" else 2
        await self.cdp_session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "everyNthFrame": every_n,
            },
        )
        self.screencast_mode = mode

    async def _screencast_mode_loop(self) -> None:
        while self.page is not None and self.cdp_session is not None:
            with contextlib.suppress(Exception):
                await self._apply_screencast_profile()
            await asyncio.sleep(0.35)

    async def _screenshot_loop(self) -> None:
        while self.page is not None:
            try:
                active = (time.monotonic() - self.last_interaction_at) < ACTIVE_WINDOW_SECONDS
                fps = ACTIVE_FPS if active else IDLE_FPS
                quality = ACTIVE_JPEG_QUALITY if active else IDLE_JPEG_QUALITY
                started = time.monotonic()
                frame = await self.page.screenshot(type="jpeg", quality=quality)
                self.frame_hub.publish(frame)
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, (1.0 / max(fps, 1.0)) - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = self._format_exception(exc)
                self.state = "error"
                LOGGER.exception("Screenshot fallback loop failed")
                break

    async def _cleanup(self, keep_error: bool, next_state: str) -> None:
        if not keep_error:
            self.last_error = ""

        for task in (self.mode_task, self.capture_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        self.mode_task = None
        self.capture_task = None

        if self.cdp_session is not None:
            with contextlib.suppress(Exception):
                await self.cdp_session.send("Page.stopScreencast")
        self.cdp_session = None

        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.close()
        self.context = None
        self.page = None

        if self.browser is not None:
            with contextlib.suppress(Exception):
                await self.browser.close()
        self.browser = None

        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        self.playwright = None

        self.capture_backend = "none"
        self.installing = False
        self.state = next_state

    def _format_exception(self, exc: BaseException) -> str:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if isinstance(exc, PlaywrightError):
            return rendered[-12000:]
        return rendered[-12000:]


manager = BrowserManager()
app = Quart(__name__)


INDEX_HTML = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --panel: #171a21;
      --panel-2: #202431;
      --line: #30384a;
      --text: #e8edf6;
      --muted: #9ba8bf;
      --accent: #4f8cff;
      --danger: #e56b6f;
      --ok: #57cc99;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, sans-serif;
    }
    body {
      display: grid;
      grid-template-rows: auto 1fr auto;
      min-height: 100vh;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      padding: 10px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      align-items: center;
      flex-wrap: wrap;
    }
    button, input {
      background: var(--panel-2);
      border: 1px solid var(--line);
      color: var(--text);
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
    }
    button {
      cursor: pointer;
    }
    button:hover { border-color: var(--accent); }
    input[type="text"] {
      flex: 1 1 420px;
      min-width: 240px;
    }
    .screen-wrap {
      position: relative;
      overflow: hidden;
      background: #000;
      min-height: 320px;
    }
    canvas {
      width: 100%;
      height: 100%;
      display: block;
      outline: none;
      cursor: default;
    }
    .statusbar {
      display: flex;
      gap: 14px;
      padding: 8px 10px;
      border-top: 1px solid var(--line);
      background: var(--panel);
      font-size: 13px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .pill {
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel-2);
      border: 1px solid var(--line);
    }
    .ok { color: var(--ok); }
    .danger { color: var(--danger); }
    .overlay {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 24px;
      background: rgba(10, 12, 16, 0.6);
      color: var(--text);
      pointer-events: none;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-size: 12px;
      line-height: 1.45;
      max-height: 28vh;
      overflow: auto;
      background: rgba(0,0,0,0.25);
      border: 1px solid var(--line);
      padding: 10px;
      border-radius: 8px;
      text-align: left;
    }
    .hint {
      color: var(--muted);
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="toolbar">
    <button id="backBtn">←</button>
    <button id="forwardBtn">→</button>
    <button id="reloadBtn">⟳</button>
    <input id="urlInput" type="text" value="{{ initial_url }}" spellcheck="false" autocomplete="off">
    <button id="goBtn">Go</button>
    <button id="restartBtn">Restart Chromium</button>
  </div>

  <div id="screenWrap" class="screen-wrap">
    <canvas id="screen" tabindex="0"></canvas>
    <div id="overlay" class="overlay">
      <div>
        <h2 id="overlayTitle">接続中...</h2>
        <div class="hint">初回は Playwright の Chromium ダウンロードで少し時間がかかります。</div>
        <br>
        <pre id="overlayLog"></pre>
      </div>
    </div>
  </div>

  <div class="statusbar">
    <span class="pill">state: <strong id="stateText">-</strong></span>
    <span class="pill">backend: <strong id="backendText">-</strong></span>
    <span class="pill">viewport: <strong id="viewportText">-</strong></span>
    <span class="pill">socket: <strong id="socketText">connecting</strong></span>
    <span class="pill">tip: 画面をクリックしてからキーボード入力</span>
  </div>

  <script>
    const canvas = document.getElementById('screen');
    const ctx = canvas.getContext('2d', { alpha: false });
    const screenWrap = document.getElementById('screenWrap');
    const overlay = document.getElementById('overlay');
    const overlayTitle = document.getElementById('overlayTitle');
    const overlayLog = document.getElementById('overlayLog');
    const urlInput = document.getElementById('urlInput');
    const stateText = document.getElementById('stateText');
    const backendText = document.getElementById('backendText');
    const viewportText = document.getElementById('viewportText');
    const socketText = document.getElementById('socketText');

    let socket;
    let remoteWidth = 0;
    let remoteHeight = 0;
    let pendingFrame = null;
    let renderBusy = false;
    let lastResize = { width: 0, height: 0 };
    let pointerPending = null;
    let pointerScheduled = false;
    let latestStatus = null;

    function setOverlay(title, log) {
      overlay.style.display = 'flex';
      overlayTitle.textContent = title;
      overlayLog.textContent = log || '';
    }

    function hideOverlay() {
      overlay.style.display = 'none';
    }

    function wsUrl() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      return `${proto}://${location.host}/ws`;
    }

    function sendWs(payload) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify(payload));
    }

    function syncViewportToServer() {
      const rect = screenWrap.getBoundingClientRect();
      const width = Math.max(320, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      if (width === lastResize.width && height === lastResize.height) return;
      lastResize = { width, height };
      sendWs({ type: 'resize', width, height });
    }

    function updateStatus(status) {
      latestStatus = status;
      stateText.textContent = status.state;
      backendText.textContent = status.capture_backend;
      viewportText.textContent = `${status.viewport.width}x${status.viewport.height}`;
      if (document.activeElement !== urlInput && status.current_url) {
        urlInput.value = status.current_url;
      }

      if (status.state === 'running') {
        hideOverlay();
      } else if (status.state === 'error') {
        setOverlay('起動エラー', status.error || 'unknown error');
      } else if (status.installing || status.state === 'installing' || status.state === 'starting') {
        setOverlay('Chromium を準備中...', status.install_log || 'しばらくお待ちください');
      } else {
        setOverlay('待機中', status.error || status.install_log || 'start/restart を試してください');
      }
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        updateStatus(await res.json());
      } catch (error) {
        setOverlay('status API error', String(error));
      }
    }

    function connectSocket() {
      socket = new WebSocket(wsUrl());
      socket.binaryType = 'arraybuffer';

      socket.addEventListener('open', () => {
        socketText.textContent = 'open';
        socketText.className = 'ok';
        syncViewportToServer();
      });

      socket.addEventListener('message', (event) => {
        if (typeof event.data === 'string') {
          const payload = JSON.parse(event.data);
          if (payload.type === 'status') {
            updateStatus(payload.status);
          }
          return;
        }

        pendingFrame = event.data;
        if (!renderBusy) {
          void renderLatestFrame();
        }
      });

      socket.addEventListener('close', () => {
        socketText.textContent = 'closed';
        socketText.className = 'danger';
        setTimeout(connectSocket, 1000);
      });

      socket.addEventListener('error', () => {
        socketText.textContent = 'error';
        socketText.className = 'danger';
      });
    }

    async function renderLatestFrame() {
      if (!pendingFrame) return;
      renderBusy = true;

      while (pendingFrame) {
        const frame = pendingFrame;
        pendingFrame = null;
        const bitmap = await createImageBitmap(new Blob([frame], { type: 'image/jpeg' }));
        if (remoteWidth !== bitmap.width || remoteHeight !== bitmap.height) {
          remoteWidth = bitmap.width;
          remoteHeight = bitmap.height;
          canvas.width = remoteWidth;
          canvas.height = remoteHeight;
          viewportText.textContent = `${remoteWidth}x${remoteHeight}`;
        }
        ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        bitmap.close();
      }

      renderBusy = false;
    }

    function toRemote(event) {
      const rect = canvas.getBoundingClientRect();
      if (!remoteWidth || !remoteHeight || !rect.width || !rect.height) {
        return { x: 0, y: 0 };
      }
      return {
        x: ((event.clientX - rect.left) / rect.width) * remoteWidth,
        y: ((event.clientY - rect.top) / rect.height) * remoteHeight,
      };
    }

    function schedulePointerMove(event) {
      const point = toRemote(event);
      pointerPending = { type: 'mouse_move', x: point.x, y: point.y };
      if (pointerScheduled) return;
      pointerScheduled = true;
      requestAnimationFrame(() => {
        pointerScheduled = false;
        if (pointerPending) {
          sendWs(pointerPending);
          pointerPending = null;
        }
      });
    }

    canvas.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      canvas.focus();
      const point = toRemote(event);
      sendWs({ type: 'mouse_down', x: point.x, y: point.y, button: event.button });
    });

    canvas.addEventListener('pointerup', (event) => {
      event.preventDefault();
      const point = toRemote(event);
      sendWs({ type: 'mouse_up', x: point.x, y: point.y, button: event.button });
    });

    canvas.addEventListener('pointermove', (event) => {
      schedulePointerMove(event);
    });

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      sendWs({ type: 'mouse_wheel', delta_x: event.deltaX, delta_y: event.deltaY });
    }, { passive: false });

    canvas.addEventListener('contextmenu', (event) => event.preventDefault());

    canvas.addEventListener('keydown', (event) => {
      event.preventDefault();
      sendWs({ type: 'keydown', key: event.key, code: event.code });
    });

    canvas.addEventListener('keyup', (event) => {
      event.preventDefault();
      sendWs({ type: 'keyup', key: event.key, code: event.code });
    });

    window.addEventListener('paste', (event) => {
      const text = event.clipboardData?.getData('text');
      if (!text || document.activeElement !== canvas) return;
      event.preventDefault();
      sendWs({ type: 'insert_text', text });
    });

    document.getElementById('goBtn').addEventListener('click', async () => {
      await fetch('/api/goto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: urlInput.value.trim() })
      });
      await refreshStatus();
    });

    urlInput.addEventListener('keydown', async (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      document.getElementById('goBtn').click();
    });

    document.getElementById('reloadBtn').addEventListener('click', async () => {
      await fetch('/api/reload', { method: 'POST' });
      await refreshStatus();
    });

    document.getElementById('backBtn').addEventListener('click', async () => {
      await fetch('/api/back', { method: 'POST' });
      await refreshStatus();
    });

    document.getElementById('forwardBtn').addEventListener('click', async () => {
      await fetch('/api/forward', { method: 'POST' });
      await refreshStatus();
    });

    document.getElementById('restartBtn').addEventListener('click', async () => {
      setOverlay('Chromium を再起動中...', '');
      await fetch('/api/restart', { method: 'POST' });
      await refreshStatus();
    });

    new ResizeObserver(() => syncViewportToServer()).observe(screenWrap);

    setInterval(refreshStatus, 2000);
    connectSocket();
    void refreshStatus();
    void fetch('/api/start', { method: 'POST' });
  </script>
</body>
</html>
"""


@app.before_serving
async def startup() -> None:
    manager.start_task = asyncio.create_task(manager.ensure_started())


@app.after_serving
async def shutdown() -> None:
    if manager.start_task is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await manager.start_task
    await manager.stop()


@app.get("/")
async def index() -> str:
    return await render_template_string(
        INDEX_HTML,
        title=APP_TITLE,
        initial_url=manager.current_url,
    )


@app.get("/api/status")
async def api_status():
    return jsonify(manager.status())


@app.post("/api/start")
async def api_start():
    if manager.start_task is None or manager.start_task.done():
        manager.start_task = asyncio.create_task(manager.ensure_started())
    await asyncio.sleep(0)
    return jsonify(manager.status())


@app.post("/api/restart")
async def api_restart():
    await manager.restart()
    return jsonify(manager.status())


@app.post("/api/stop")
async def api_stop():
    await manager.stop()
    return jsonify(manager.status())


@app.post("/api/goto")
async def api_goto():
    payload = await request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    await manager.goto(url)
    return jsonify({"ok": True, "status": manager.status()})


@app.post("/api/reload")
async def api_reload():
    await manager.reload()
    return jsonify({"ok": True, "status": manager.status()})


@app.post("/api/back")
async def api_back():
    await manager.back()
    return jsonify({"ok": True, "status": manager.status()})


@app.post("/api/forward")
async def api_forward():
    await manager.forward()
    return jsonify({"ok": True, "status": manager.status()})


@app.websocket("/ws")
async def ws_endpoint() -> None:
    client = manager.frame_hub.register()

    async def sender() -> None:
        while True:
            frame = await client.queue.get()
            await websocket.send(frame)

    send_task = asyncio.create_task(sender())
    try:
        await websocket.send(json.dumps({"type": "status", "status": manager.status()}))
        while True:
            raw = await websocket.receive()
            if raw is None:
                break
            if isinstance(raw, bytes):
                continue
            payload = json.loads(raw)
            await manager.handle_client_event(payload)
    except Exception:
        pass
    finally:
        send_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await send_task
        manager.frame_hub.unregister(client)


if __name__ == "__main__":
    LOGGER.info("%s listening on 0.0.0.0:%s", APP_TITLE, DEFAULT_PORT)
    LOGGER.info("PLAYWRIGHT_BROWSERS_PATH=%s", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    LOGGER.info("Log file=%s", LOG_FILE)
    LOGGER.info("Temp directory=%s", TEMP_DIR)
    app.run(host="0.0.0.0", port=DEFAULT_PORT)