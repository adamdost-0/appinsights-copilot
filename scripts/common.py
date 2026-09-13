"""Shared checked commands and private runtime state."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
AZURE_STATE = LOCAL / "azure.json"


class AppError(RuntimeError):
    """An actionable operational failure safe to display after redaction."""


def redact(text: str, env: dict[str, str] | None = None) -> str:
    for name, value in (os.environ if env is None else env).items():
        if value and re.search(r"SECRET|TOKEN|PASSWORD|CONNECTION_STRING|API_KEY", name, re.I):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"InstrumentationKey=[^\s\"']+", "[REDACTED_CONNECTION_STRING]", text, flags=re.I)
    return re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)


def run(args: list[str], *, env: dict[str, str] | None = None,
        timeout: float = 120, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, env=env,
                                timeout=timeout, cwd=cwd, check=False)
    except FileNotFoundError as error:
        raise AppError(f"Required executable not found: {args[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise AppError(f"{args[0]} timed out after {timeout:g} seconds") from error
    except OSError as error:
        raise AppError(f"Cannot execute {args[0]}: {error.strerror}") from error
    if result.returncode:
        detail = redact(result.stderr or result.stdout or "No diagnostic output", env)
        raise AppError(f"{args[0]} failed (exit {result.returncode}): {detail.strip()}")
    return result


def run_json(args: list[str], **kwargs):
    result = run(args, **kwargs)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AppError(f"{args[0]} returned invalid JSON") from error


def run_session(args: list[str], *, env: dict[str, str] | None = None,
                timeout: float = 180, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """A CLI invocation may spawn subagents; terminate its whole owned group on timeout."""
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env, cwd=cwd, start_new_session=True)
    except OSError as error:
        raise AppError(f"Cannot execute {args[0]}: {error.strerror}") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        deadline = time.monotonic() + 5
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # The process group may exit between the timeout and the signal.
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(min(0.05, remaining))
        process.communicate()
        raise AppError(f"{args[0]} timed out after {timeout:g} seconds; owned process group stopped") from error
    if process.returncode:
        detail = redact(stderr or stdout or "No diagnostic output", env)
        raise AppError(f"{args[0]} failed (exit {process.returncode}): {detail.strip()}")
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def private_dir(path: Path) -> Path:
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise AppError(f"Refusing symlink in private state path: {item}")
    try:
        missing = [item for item in (path, *path.parents) if not item.exists()]
        for item in reversed(missing):
            item.mkdir(mode=0o700, exist_ok=True)
            item.chmod(0o700)
        path.chmod(0o700)
    except OSError as error:
        raise AppError(f"Cannot create private directory {path}: {error.strerror}") from error
    return path


def write_private(path: Path, content: str) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise AppError(f"Refusing symlink state file: {path}")
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            name = stream.name
            os.chmod(name, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except OSError as error:
        raise AppError(f"Cannot write private file {path}: {error.strerror}") from error
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def write_json(path: Path, data: dict) -> None:
    write_private(path, json.dumps(data, indent=2) + "\n")


def load_json(path: Path) -> dict:
    if path.is_symlink():
        raise AppError(f"Refusing symlink state file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AppError(f"Cannot read JSON state at {path}: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise AppError(f"Expected JSON object at {path}")
    return value


def validate_run_id(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise AppError("Run ID must be a UUID") from error
    if canonical != value:
        raise AppError("Run ID must be a canonical lowercase UUID")
    return value
