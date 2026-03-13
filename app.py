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
LOCAL_FONTS_DIR = Path(os.environ.get("PYMIUM_LOCAL_FONT_DIR", str(BASE_DIR / ".pymium-fonts"))).resolve()
FONTCONFIG_DIR = Path(os.environ.get("PYMIUM_FONTCONFIG_DIR", str(CACHE_DIR / "fontconfig"))).resolve()
FONTCONFIG_FILE = FONTCONFIG_DIR / "fonts.conf"
PLAYWRIGHT_BROWSERS_DIR = Path(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / ".playwright-browsers"))
).resolve()

for directory in (TEMP_DIR, CACHE_DIR, LOG_DIR, LOCAL_LIBS_DIR, LOCAL_FONTS_DIR, FONTCONFIG_DIR, PLAYWRIGHT_BROWSERS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_DIR)
for env_key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
    os.environ[env_key] = str(TEMP_DIR)
os.environ.setdefault("PIP_CACHE_DIR", str((CACHE_DIR / "pip").resolve()))
os.environ.setdefault("XDG_CACHE_HOME", str((CACHE_DIR / "xdg").resolve()))
Path(os.environ["PIP_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
os.environ["FONTCONFIG_PATH"] = str(FONTCONFIG_DIR)
os.environ["FONTCONFIG_FILE"] = str(FONTCONFIG_FILE)
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

ROOTLESS_DEBIAN_COMMON_PACKAGES = [
    "libasound2",
    "libatk-bridge2.0-0",
    "libatk1.0-0",
    "libatspi2.0-0",
    "libcairo2",
    "libcups2",
    "libdbus-1-3",
    "libdrm2",
    "libgbm1",
    "libglib2.0-0",
    "libgtk-3-0",
    "libnspr4",
    "libnss3",
    "libpango-1.0-0",
    "libpangocairo-1.0-0",
    "libx11-6",
    "libx11-xcb1",
    "libxcb1",
    "libxcomposite1",
    "libxdamage1",
    "libxext6",
    "libxfixes3",
    "libxkbcommon0",
    "libxrandr2",
    "libxrender1",
    "libxshmfence1",
]

ROOTLESS_DEBIAN_FONT_PACKAGES = [
    "fonts-noto-cjk",
]

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
if SCREENCAST_WEBP:
    RUNTIME_REQUIREMENTS["PIL"] = "Pillow>=10.0"


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
    normalized_library_name = library_name.lower()
    package_manager = detect_package_manager()
    package_candidates = SYSTEM_PACKAGE_MAP.get(package_manager or "", {}).get(normalized_library_name, [])
    hints = SHARED_LIBRARY_HINTS.get(
        normalized_library_name,
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
# スクリーンショットfallbackの形式 (jpeg / webp / png)
_sf = os.environ.get("SCREENSHOT_FORMAT", "webp").lower()
SCREENSHOT_FORMAT: str = _sf if _sf in ("jpeg", "webp", "png") else "webp"
# CDP screencastフレームをWebPに変換するか (Pillowが必要)
SCREENCAST_WEBP = os.environ.get("SCREENCAST_WEBP", "0").strip().lower() not in {"0", "false", "no", "off"}
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


def discover_font_dirs(base_dir: Path) -> list[str]:
    if not base_dir.exists():
        return []

    font_extensions = (".ttf", ".otf", ".ttc", ".otc")
    directories: list[str] = []
    for root, _, files in os.walk(base_dir):
        if any(file_name.lower().endswith(font_extensions) for file_name in files):
            root_path = str(Path(root).resolve())
            if root_path not in directories:
                directories.append(root_path)
    return directories


def write_fontconfig_file() -> None:
    font_dirs = [
        *discover_font_dirs(LOCAL_FONTS_DIR),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
    ]
    unique_dirs: list[str] = []
    for path in font_dirs:
        if path and path not in unique_dirs:
            unique_dirs.append(path)

    dir_xml = "\n".join(f"  <dir>{path}</dir>" for path in unique_dirs)
    content = f"""<?xml version=\"1.0\"?>
<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">
<fontconfig>
{dir_xml}
  <cachedir>{(CACHE_DIR / 'fontconfig-cache').resolve()}</cachedir>

  <alias>
    <family>sans-serif</family>
    <prefer>
      <family>Noto Sans CJK JP</family>
      <family>Noto Sans JP</family>
      <family>IPAGothic</family>
    </prefer>
  </alias>

  <alias>
    <family>serif</family>
    <prefer>
      <family>Noto Serif CJK JP</family>
      <family>Noto Sans CJK JP</family>
    </prefer>
  </alias>

  <alias>
    <family>monospace</family>
    <prefer>
      <family>Noto Sans Mono CJK JP</family>
      <family>Noto Sans CJK JP</family>
    </prefer>
  </alias>
</fontconfig>
"""
    FONTCONFIG_FILE.write_text(content, encoding="utf-8")


write_fontconfig_file()


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
    target = library_name.lower()
    for root, _, files in os.walk(LOCAL_LIBS_DIR):
        if any(file_name.lower() == target for file_name in files):
            return True
    return False


def japanese_font_bundle_present() -> bool:
    return bool(discover_font_dirs(LOCAL_FONTS_DIR))


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
        self.missing_shared_library: Optional[s
