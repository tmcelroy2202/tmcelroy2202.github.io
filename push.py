#!/usr/bin/env python3
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
import paramiko
import pathspec
from playwright.sync_api import sync_playwright
import socks

# --- 1. Load Configuration ---
def load_env():
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")

load_env()

VPN_HOST = "vpn.charlotte.edu"
REMOTE_HOST = os.getenv("REMOTE_HOST", "webpages.uncc.edu")
REMOTE_PORT = int(os.getenv("REMOTE_PORT", "22"))
REMOTE_USER = os.getenv("REMOTE_USER", "tmcelro3")
REMOTE_PASS = os.getenv("REMOTE_PASSWORD", "")
REMOTE_PATH = os.getenv("REMOTE_PATH", "/public_html").rstrip("/")
LOCAL_PATH = Path(os.getenv("LOCAL_PATH", ".")).resolve()

BROWSER_STATE_FILE = Path(".browser_state.json")
DRY_RUN = "--dry-run" in sys.argv
SOCKS_PORT = 11080


# --- 2. Headless SSO Token Acquisition ---
def acquire_webvpn_cookie():
    print("==> Refreshing VPN session via SSO...")
    captured_cookie = None
    printed_duo_code = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        if BROWSER_STATE_FILE.exists():
            try:
                context = browser.new_context(storage_state=str(BROWSER_STATE_FILE))
            except Exception:
                context = browser.new_context()
        else:
            context = browser.new_context()

        page = context.new_page()

        context.add_cookies([
            {"name": "tg", "value": "0VU5DQy1EZWZhdWx0", "domain": VPN_HOST, "path": "/"},
            {"name": "webvpnlogin", "value": "1", "domain": VPN_HOST, "path": "/"}
        ])

        logon_url = f"https://{VPN_HOST}/+CSCOE+/logon.html?tgroup=UNCC-Default"
        page.goto(logon_url)

        # 1. Fill Username if prompted
        try:
            email_field = page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=3000)
            if email_field:
                user_email = f"{REMOTE_USER}@charlotte.edu" if "@" not in REMOTE_USER else REMOTE_USER
                email_field.fill(user_email)
                next_btn = page.query_selector('input[type="submit"][value="Next"], #idSIButton9')
                if next_btn:
                    next_btn.click()
                else:
                    page.keyboard.press("Enter")
        except Exception:
            pass

        # 2. Fill Password if prompted
        try:
            pwd_field = page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=3000)
            if pwd_field and REMOTE_PASS:
                pwd_field.fill(REMOTE_PASS)
                time.sleep(0.3)
                signin_btn = page.query_selector('input[type="submit"][value="Sign in"], #idSIButton9')
                if signin_btn:
                    signin_btn.click()
                else:
                    page.keyboard.press("Enter")
        except Exception:
            pass

        # 3. Interstitials & Duo Screen Handler
        def click_interstitials():
            all_targets = [page] + page.frames
            selectors = [
                'button:has-text("Yes, this is my device")',
                'button:has-text("Continue")',
                'input[type="submit"][value="Continue"]',
                'button:has-text("Verify")',
                'input[type="submit"][value="Verify"]',
                'input[type="submit"][value="Yes"]',
                '#idSIButton9'
            ]
            for target in all_targets:
                for sel in selectors:
                    try:
                        btn = target.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            break
                    except Exception:
                        pass

        # 4. Extract Duo Code if prompt appears & Poll for Token
        timeout_seconds = 60
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            click_interstitials()

            if not printed_duo_code:
                all_targets = [page] + page.frames
                for target in all_targets:
                    try:
                        code_el = target.query_selector("span.code-text")
                        if code_el and code_el.is_visible():
                            code = code_el.inner_text().strip()
                            if code:
                                print("\n" + "=" * 40)
                                print(f"    DUO VERIFICATION CODE: {code}")
                                print("=" * 40 + "\n")
                                printed_duo_code = True
                                break
                    except Exception:
                        pass

            cookies = context.cookies()
            for c in cookies:
                if c["domain"] in [VPN_HOST, f".{VPN_HOST}"] and c["name"] == "webvpn" and c["value"]:
                    captured_cookie = f"webvpn={c['value']}"
                    break

            if captured_cookie:
                break

            time.sleep(0.4)

        try:
            context.storage_state(path=str(BROWSER_STATE_FILE))
        except Exception:
            pass

        browser.close()

    if not captured_cookie:
        raise TimeoutError("Failed to obtain AnyConnect session cookie.")

    print("[+] New VPN session token established.")
    return captured_cookie


# --- 3. User-Space VPN Tunnel (ocproxy) ---
class UserSpaceVPN:
    def __init__(self, cookie, socks_port):
        self.cookie = cookie
        self.socks_port = socks_port
        self.proc = None

    def start(self):
        cmd = [
            "openconnect",
            "--cookie-on-stdin",
            "-S",
            "-s", f"ocproxy -D {self.socks_port} -v",
            VPN_HOST
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        self.proc.stdin.write(f"{self.cookie}\n")
        self.proc.stdin.flush()

        tunnel_ready = False
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            clean_line = line.strip()
            if clean_line:
                print(f"  [tunnel] {clean_line}")

            if any(term in clean_line.lower() for term in [
                "failed to complete authentication",
                "cookie was rejected by server",
                "401 unauthorized"
            ]):
                self.stop()
                return False

            if any(key in clean_line.lower() for key in ["listening on port", "socks proxy on", "proxy listening"]):
                tunnel_ready = True
                break

            if "session authentication will expire" in clean_line.lower():
                time.sleep(1.2)
                tunnel_ready = True
                break

        if not tunnel_ready:
            self.stop()
            return False

        print(f"[+] User-space proxy ready at 127.0.0.1:{self.socks_port}")
        return True

    def stop(self):
        if self.proc:
            print("==> Tearing down user-space tunnel...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


# --- 4. Gitignore-aware SFTP Sync with Mirror & Deletion ---
def load_gitignore(base_dir):
    patterns = [
        ".git/",
        ".git/**",
        ".gitignore",
        ".vpn_cookie",
        ".browser_state.json",
        ".env*",
        "push.py",
        "sftppush.sh",
        "__pycache__/"
    ]
    gi = base_dir / ".gitignore"
    if gi.exists():
        with open(gi) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def is_ignored(rel_path_str, is_dir, matcher):
    norm = rel_path_str.replace("\\", "/")
    if is_dir and not norm.endswith("/"):
        norm += "/"
    return matcher.match_file(norm)


def sync_files():
    matcher = load_gitignore(LOCAL_PATH)

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, "127.0.0.1", SOCKS_PORT)
    print(f"==> Connecting to {REMOTE_HOST}:{REMOTE_PORT} via SOCKS5 proxy...")
    sock.connect((REMOTE_HOST, REMOTE_PORT))

    transport = paramiko.Transport(sock)
    transport.connect(username=REMOTE_USER, password=REMOTE_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)

    print(f"==> Mirroring {LOCAL_PATH} -> {REMOTE_PATH}")

    # Phase 1: Upload and Update Local Files
    for root, dirs, files in os.walk(LOCAL_PATH):
        rel_root = Path(root).relative_to(LOCAL_PATH)

        # Skip ignored directories locally
        dirs[:] = [
            d for d in dirs
            if not is_ignored(str(rel_root / d), is_dir=True, matcher=matcher)
        ]

        # Ensure directory exists remotely
        target_dir = f"{REMOTE_PATH}/{rel_root}".replace("\\", "/").rstrip("/.")
        if target_dir:
            parts = target_dir.strip("/").split("/")
            cur = ""
            for p in parts:
                cur += "/" + p
                try:
                    sftp.stat(cur)
                except IOError:
                    if not DRY_RUN:
                        sftp.mkdir(cur)
                    print(f"[mkdir] {cur}")

        for fname in files:
            rel_file = rel_root / fname
            rel_file_str = str(rel_file).replace("\\", "/")

            if is_ignored(rel_file_str, is_dir=False, matcher=matcher):
                continue

            local_fpath = Path(root) / fname
            remote_fpath = f"{target_dir}/{fname}" if target_dir else f"{REMOTE_PATH}/{fname}"

            needs_upload = True
            try:
                r_stat = sftp.stat(remote_fpath)
                l_stat = local_fpath.stat()
                if r_stat.st_size == l_stat.st_size and int(r_stat.st_mtime) >= int(l_stat.st_mtime):
                    needs_upload = False
            except IOError:
                pass

            if needs_upload:
                if DRY_RUN:
                    print(f"[dry-run] put {fname} -> {remote_fpath}")
                else:
                    print(f"[put] {fname} -> {remote_fpath}")
                    sftp.put(str(local_fpath), remote_fpath)
                    sftp.chmod(remote_fpath, 0o644)

    # Phase 2: Purge Remote Files and Directories that No Longer Exist Locally or Are Ignored
    print("==> Checking for orphaned remote files to delete...")

    def purge_remote_directory(current_remote_dir):
        try:
            entries = sftp.listdir_attr(current_remote_dir)
        except IOError:
            return

        for attr in entries:
            fname = attr.filename
            if fname in [".", ".."]:
                continue

            item_remote_path = f"{current_remote_dir}/{fname}".replace("//", "/")
            rel_path_str = os.path.relpath(item_remote_path, REMOTE_PATH).replace("\\", "/")
            local_equiv = LOCAL_PATH / rel_path_str
            is_dir = stat.S_ISDIR(attr.st_mode)

            # Delete if file/dir is ignored OR deleted locally
            should_delete = is_ignored(rel_path_str, is_dir=is_dir, matcher=matcher) or not local_equiv.exists()

            if is_dir:
                # Recurse first before attempting to remove directory
                purge_remote_directory(item_remote_path)
                if should_delete:
                    if DRY_RUN:
                        print(f"[dry-run] rmdir {item_remote_path}")
                    else:
                        try:
                            sftp.rmdir(item_remote_path)
                            print(f"[rmdir] {item_remote_path}")
                        except IOError as e:
                            print(f"[-] Could not rmdir {item_remote_path}: {e}")
            else:
                if should_delete:
                    if DRY_RUN:
                        print(f"[dry-run] rm {item_remote_path}")
                    else:
                        try:
                            sftp.remove(item_remote_path)
                            print(f"[rm] {item_remote_path}")
                        except IOError as e:
                            print(f"[-] Could not rm {item_remote_path}: {e}")

    purge_remote_directory(REMOTE_PATH)

    sftp.close()
    transport.close()


# --- 5. Main Flow ---
def main():
    if DRY_RUN:
        print("[DRY RUN MODE ENABLED]")

    cookie = acquire_webvpn_cookie()
    vpn = UserSpaceVPN(cookie, SOCKS_PORT)

    if not vpn.start():
        raise ConnectionError("Tunnel failed to start with freshly captured session token.")

    try:
        sync_files()
        print("==> Sync complete successfully!")
    finally:
        vpn.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
