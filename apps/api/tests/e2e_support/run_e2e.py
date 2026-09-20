import os
import sys
import time
import uuid
import shutil
import hashlib
import pathlib
import urllib.request
import subprocess
from tests.e2e_support.seed_e2e import SENTINEL_FILENAME, seed_e2e_database

def compute_file_hash(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def get_file_stats(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"exists": False, "size": 0, "mtime": 0, "hash": None}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "hash": compute_file_hash(path)
    }

def poll_url(url: str, timeout_seconds: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "E2E-HealthChecker"})
            with urllib.request.urlopen(req, timeout=2) as res:
                if res.status in (200, 304):
                    return True
        except Exception:
            time.sleep(0.5)
    return False

def find_free_port(preferred_port: int, host: str = "127.0.0.1") -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred_port))
            return preferred_port
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]

def terminate_process_tree(proc: subprocess.Popen | None, name: str) -> None:
    if not proc:
        return
    if proc.poll() is not None:
        return

    print(f"Terminating {name} (PID={proc.pid})...")
    if os.name == "nt":
        try:
            # taskkill /F /T kills the process and all descendant children
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception as e:
            print(f"Warning: error terminating {name}: {e}", file=sys.stderr)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
    else:
        import signal
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"{name} did not exit within timeout, sending SIGKILL...", file=sys.stderr)
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"Warning: error terminating process group for {name}: {e}", file=sys.stderr)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

def main():
    current_file = pathlib.Path(__file__).resolve()
    api_dir = current_file.parent.parent.parent  # apps/api
    web_dir = api_dir.parent / "web"  # apps/web
    dev_db_path = api_dir / "tradepro.db"

    print("=== TradePro Milestone 5C E2E Orchestrator ===")

    api_port = int(os.getenv("E2E_API_PORT", str(find_free_port(8000))))
    web_port = int(os.getenv("E2E_WEB_PORT", str(find_free_port(3000))))

    print(f"Using E2E Ports: FastAPI={api_port}, Next.js={web_port}")

    # 1. Capture primary dev db integrity baseline
    initial_db_stats = get_file_stats(dev_db_path)
    print(f"Captured development DB baseline: exists={initial_db_stats['exists']}, size={initial_db_stats['size']}, hash={initial_db_stats['hash']}")

    # 2. Create isolated disposable temp directory with sentinel
    import tempfile
    unique_id = uuid.uuid4().hex
    temp_dir = pathlib.Path(tempfile.gettempdir()) / f"tradepro_e2e_{unique_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    sentinel_file = temp_dir / SENTINEL_FILENAME
    sentinel_file.touch()

    db_path = temp_dir / "e2e_disposable.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    manifest_path = temp_dir / "e2e_manifest.json"

    print(f"Created ephemeral E2E environment at: {temp_dir}")
    print(f"Disposable database URL: {db_url}")

    e2e_env = os.environ.copy()
    e2e_env["APP_ENV"] = "test"
    e2e_env["DATABASE_URL"] = db_url
    e2e_env["E2E_TEMP_DIR"] = str(temp_dir)
    e2e_env["E2E_MANIFEST_PATH"] = str(manifest_path)
    e2e_env["COOKIE_SECURE"] = "false"
    e2e_env["ALLOWED_ORIGINS"] = f"http://127.0.0.1:{web_port},http://localhost:{web_port}"
    e2e_env["NEXT_PUBLIC_API_URL"] = f"http://127.0.0.1:{api_port}"
    e2e_env["PLAYWRIGHT_BASE_URL"] = f"http://127.0.0.1:{web_port}"

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    api_proc = None
    web_proc = None
    exit_code = 1

    try:
        # 3. Run Alembic upgrade head against disposable database
        print("\n--- Running Alembic Upgrade Head ---")
        alembic_cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
        subprocess.check_call(alembic_cmd, cwd=str(api_dir), env=e2e_env)

        # 4. Seed random test users and write manifest
        print("\n--- Seeding Disposable Test Data ---")
        seed_cmd = [sys.executable, "-m", "tests.e2e_support.seed_e2e"]
        subprocess.check_call(seed_cmd, cwd=str(api_dir), env=e2e_env)

        # 5. Start FastAPI backend process
        print(f"\n--- Starting FastAPI Backend Process on Port {api_port} ---")
        api_cmd = [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(api_port)]
        api_proc = subprocess.Popen(api_cmd, cwd=str(api_dir), env=e2e_env, **popen_kwargs)

        print("Polling FastAPI backend readiness...")
        if not poll_url(f"http://127.0.0.1:{api_port}/health", timeout_seconds=20):
            raise RuntimeError(f"FastAPI backend failed to become ready at http://127.0.0.1:{api_port}/health")
        print("FastAPI backend is ready.")

        # 6. Build Next.js
        print("\n--- Building Next.js Web App ---")
        build_cmd = ["npm.cmd" if os.name == "nt" else "npm", "run", "build"]
        subprocess.check_call(build_cmd, cwd=str(web_dir), env=e2e_env)

        # 7. Start Next.js production server
        print(f"\n--- Starting Next.js Web Server on Port {web_port} ---")
        start_cmd = ["npm.cmd" if os.name == "nt" else "npm", "run", "start", "--", "-p", str(web_port), "-H", "127.0.0.1"]
        web_proc = subprocess.Popen(start_cmd, cwd=str(web_dir), env=e2e_env, **popen_kwargs)

        print("Polling Next.js frontend readiness...")
        if not poll_url(f"http://127.0.0.1:{web_port}", timeout_seconds=20):
            raise RuntimeError(f"Next.js frontend failed to become ready at http://127.0.0.1:{web_port}")
        print("Next.js frontend is ready.")

        # 8. Execute Playwright E2E Test Suite (forwarding any CLI arguments)
        print("\n--- Executing Playwright Test Suite ---")
        extra_args = sys.argv[1:]
        test_cmd = ["npx.cmd" if os.name == "nt" else "npx", "playwright", "test"] + extra_args
        test_res = subprocess.run(test_cmd, cwd=str(web_dir), env=e2e_env)
        exit_code = test_res.returncode

    except Exception as e:
        print(f"\n[E2E ERROR]: {e}", file=sys.stderr)
        exit_code = 1

    finally:
        # 9. Clean up running processes and ensure complete exit
        print("\n--- Shutting Down Test Servers ---")
        terminate_process_tree(web_proc, "Next.js frontend")
        terminate_process_tree(api_proc, "FastAPI backend")

        # 10. Verify primary development DB was untouched
        final_db_stats = get_file_stats(dev_db_path)
        print(f"Verifying development DB integrity post-run: exists={final_db_stats['exists']}, hash={final_db_stats['hash']}")

        if initial_db_stats["exists"] != final_db_stats["exists"] or initial_db_stats["hash"] != final_db_stats["hash"]:
            print("CRITICAL ERROR: Primary development tradepro.db was modified during E2E run!", file=sys.stderr)
            exit_code = 1

        # 11. Unconditionally remove temporary directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"Cleaned up ephemeral directory: {temp_dir}")

        # 12. Clean up any local auth storage state
        auth_storage_dir = web_dir / "test-results" / ".auth"
        if auth_storage_dir.exists():
            shutil.rmtree(auth_storage_dir, ignore_errors=True)
            print("Cleaned up local auth storage-state directory.")

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
