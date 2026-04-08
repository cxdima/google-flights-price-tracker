"""
Chrome / Selenium lifecycle management.

Key goals:
  - Stealth: pass Google's bot-detection (JS patches via CDP)
  - Memory: disable images, cap renderer processes and V8 heap
  - Reliability: nuke leftover processes before each launch
"""
import glob
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService

from config import CHROME_BINARY, CHROMEDRIVER_PATH, CHROME_USER_AGENT

log = logging.getLogger(__name__)

# JavaScript injected before every page to hide automation fingerprints
_STEALTH_JS = """
// ── webdriver flag ─────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// ── plugins — headless Chrome normally reports 0 ───────────────────────────
Object.defineProperty(navigator, 'plugins', {
  get: () => ({
    0: { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',      description: 'Portable Document Format' },
    1: { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    2: { name: 'Native Client',      filename: 'internal-nacl-plugin',     description: '' },
    length: 3
  })
});

// ── languages ──────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// ── chrome runtime object (absent in headless without this patch) ──────────
window.chrome = {
  app:     { isInstalled: false, InstallState: { DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' }, RunningState: { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' } },
  runtime: {
    PlatformOs:   { MAC:'mac', WIN:'win', ANDROID:'android', CROS:'cros', LINUX:'linux', OPENBSD:'openbsd' },
    PlatformArch: { ARM:'arm', X86_32:'x86-32', X86_64:'x86-64' },
    PlatformNaclArch: { ARM:'arm', X86_32:'x86-32', X86_64:'x86-64' },
    RequestUpdateCheckStatus: { THROTTLED:'throttled', NO_UPDATE:'no_update', UPDATE_AVAILABLE:'update_available' },
    OnInstalledReason:  { INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update' },
    OnRestartRequiredReason: { APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic' },
    connect: function() {},
    sendMessage: function() {}
  },
  loadTimes: function() { return {}; },
  csi:       function() { return {}; }
};

// ── permissions — avoid fingerprinting via query() ─────────────────────────
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
  p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : _origQuery(p);
"""


def nuke_chrome() -> None:
    """Kill any leftover Chrome / Chromedriver processes and temp dirs."""
    killed = False
    for proc in ("chromium", "chrome", "chromedriver"):
        try:
            result = subprocess.run(["pkill", "-9", proc], capture_output=True)
            if result.returncode == 0:
                killed = True
        except Exception:
            pass

    for d in glob.glob("/tmp/gfpt-chrome-*"):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    # Only sleep if we actually killed something — skip on clean Lambda starts
    if killed:
        time.sleep(0.2)


def build_driver() -> webdriver.Chrome:
    """
    Launch a headless Chrome instance tuned for Lambda:
      - Minimal memory footprint (images off, capped renderer + V8 heap)
      - Stealth fingerprint (CDP JS patches applied before any page loads)
      - Eager page-load strategy (don't wait for every last resource)
    """
    profile_dir = f"/tmp/gfpt-chrome-{uuid.uuid4().hex}"
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    opts = Options()

    # ── Headless / sandbox ────────────────────────────────────────────────────
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")

    # ── Stealth: remove automation tells ─────────────────────────────────────
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # ── Realistic fingerprint ─────────────────────────────────────────────────
    opts.add_argument(f"--user-agent={CHROME_USER_AGENT}")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=en-US,en;q=0.9")
    opts.add_argument("--accept-lang=en-US,en;q=0.9")

    # ── Container / Lambda stability ──────────────────────────────────────────
    # --single-process: run browser + renderer in one OS process.
    #   Eliminates the inter-process "Unable to receive message from renderer"
    #   / "tab crashed" failures that occur in Lambda's container because the
    #   zygote pre-fork or the renderer sandbox cannot initialise cleanly.
    #   This is safe here: we run exactly one task per invocation with --no-sandbox
    #   already set, so the isolation trade-off is acceptable.
    # --disable-crash-reporter: no crash upload attempts (reduces noise + hangs)
    #
    # NOTE: --no-zygote is intentionally omitted. It breaks JavaScript-based
    #   redirect chains (e.g. flights/saves → accounts.google.com) because
    #   Chrome can no longer hand off renderer work through the zygote, causing
    #   navigations to stall silently before the login page finishes loading.
    #
    # NOTE: --force-color-profile=srgb is intentionally omitted.
    #   In some Lambda container images it triggers a colour-management crash
    #   during renderer init, manifesting as "Unable to receive message from
    #   renderer" at NEW_SESSION. Leave it out; headless mode picks a safe default.
    opts.add_argument("--single-process")
    opts.add_argument("--disable-crash-reporter")

    # ── Memory optimisations ──────────────────────────────────────────────────
    # NOTE: --renderer-process-limit is intentionally omitted.
    #   Capping to 1 renderer means a single JS crash kills the entire browser
    #   session (Chrome cannot recover without a spare renderer slot). Google
    #   Flights is a heavy SPA and will occasionally trigger a renderer OOM.
    #
    # NOTE: --js-flags=--max-old-space-size is intentionally omitted.
    #   256 MB is too small for Google Flights' JS bundle; V8 would OOM and
    #   crash the renderer, causing the "invalid session id" error.
    opts.add_argument("--blink-settings=imagesEnabled=false")   # skip image decode
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-translate")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-hang-monitor")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-component-update")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--safebrowsing-disable-auto-update")

    # ── Profile / cache (fresh per invocation, in /tmp) ───────────────────────
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument(f"--disk-cache-dir={profile_dir}/cache")
    opts.add_argument("--disk-cache-size=1")

    # ── CDP performance logs (needed for network interception) ─────────────────
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.page_load_strategy = "eager"

    opts.binary_location = CHROME_BINARY

    service = ChromeService(executable_path=CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=opts)

    _apply_stealth(driver)
    log.info("Chrome launched (profile=%s)", profile_dir)
    return driver


def _apply_stealth(driver: webdriver.Chrome) -> None:
    """Inject stealth JS so it runs before every page's own scripts."""
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": _STEALTH_JS},
    )
