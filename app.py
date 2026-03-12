import asyncio
import base64
import contextlib
import importlib
import json
import logging
import os
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
PLAYWRIGHT_BROWSERS_DIR = Path(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / ".playwright-browsers"))
).resolve()

for directory in (TEMP_DIR, CACHE_DIR, LOG_DIR, PLAYWRIGHT_BROWSERS_DIR):
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
        }

    def touch(self) -> None:
        self.last_interaction_at = time.monotonic()

    def _handle_frame_navigated(self, frame: Any) -> None:
        if self.page is not None and frame == self.page.main_frame:
            self.current_url = frame.url

    async def install_browser_runtime(self) -> None:
        self.installing = True
        self.install_log = ""
        self.state = "installing"
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

    async def ensure_started(self) -> None:
        async with self.start_lock:
            if self.page is not None and self.state in {"running", "installing", "starting"}:
                return

            await self._cleanup(keep_error=False, next_state="starting")
            self.warning = ""
            self.last_error = ""
            self.state = "starting"

            try:
                await self.install_browser_runtime()

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
            except Exception as exc:
                self.last_error = self._format_exception(exc)
                LOGGER.exception("Failed to start Chromium session")
                await self._cleanup(keep_error=True, next_state="error")

    async def restart(self) -> None:
        LOGGER.info("Restarting Chromium session")
        async with self.start_lock:
            await self._cleanup(keep_error=False, next_state="restarting")
        await self.ensure_started()

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