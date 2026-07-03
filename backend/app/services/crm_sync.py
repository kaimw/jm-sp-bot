from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import AuditEvent, ChannelPricing, CrmOrderItem, CrmOrderSnapshot, CrmSalesOrder, OrderAttachment, ProductInventorySnapshot, ProductSKU, ProductSPU, PromotionRule, CrmSyncRun, ProcessingJob, SystemConfig, now_utc
from backend.app.services.crm_attachment_cache import cache_order_attachment_file
from backend.app.services.bootstrap import set_config
from backend.app.services.crypto import decrypt_value
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.order_region import DOMESTIC_SETTLEMENT_METHOD, is_overseas_order_payload
from backend.app.services.order_middle_platform import enqueue_crm_order_parsed_event

logger = logging.getLogger(__name__)


DEFAULT_SOURCE_SYSTEM = "fxiaoke"
DEFAULT_CDP_URL = "http://127.0.0.1:9333"
FXIAOKE_LOGIN_STATE_FILE = Path("/private/tmp/fxiaoke-login-renewal-state.json")
FXIAOKE_LOGIN_COOLDOWN_SECONDS = 10 * 60
CRM_SYNC_LOCK_MESSAGE = "当前有 CRM 同步任务正在进行，请稍后重试。"
CRM_SYNC_LOCK_STALE_MESSAGE = "检测到上一次 CRM 同步锁超时，已自动释放并继续执行。"
CRM_SYNC_LOCK = threading.Lock()
CRM_SYNC_LOCK_STATE: dict[str, Any] = {}

# 单订单详情同步使用独立的锁字典，不同订单可以并行
CRM_DETAIL_LOCK = threading.Lock()  # 保护 _detail_locks 字典的元锁
_detail_locks: dict[str, threading.Lock] = {}
MAX_CONCURRENT_DETAIL_SYNCS = 3  # 最多同时进行3个详情同步（避免 CDP 浏览器压力过大）
_detail_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_DETAIL_SYNCS)


class CrmSyncBusyError(RuntimeError):
    pass


def crm_sync_lock_lease_seconds(session: Session) -> int:
    configured_timeout = max(30, config_int(session, "crm_sync_timeout_seconds", 120))
    return max(180, min(900, configured_timeout + 90))


@contextmanager
def crm_sync_lock(session: Session, operation: str):
    lease_seconds = crm_sync_lock_lease_seconds(session)
    acquired = CRM_SYNC_LOCK.acquire(blocking=False)
    if not acquired:
        now = time.time()
        acquired_at = float(CRM_SYNC_LOCK_STATE.get("acquired_at") or 0)
        age_seconds = int(now - acquired_at) if acquired_at > 0 else None
        if age_seconds is not None and age_seconds > lease_seconds:
            try:
                CRM_SYNC_LOCK.release()
            except RuntimeError:
                pass
            acquired = CRM_SYNC_LOCK.acquire(blocking=False)
            if acquired:
                CRM_SYNC_LOCK_STATE.clear()
                CRM_SYNC_LOCK_STATE.update(
                    {
                        "operation": operation,
                        "acquired_at": now,
                        "lease_seconds": lease_seconds,
                        "stale_lock_recovered": True,
                    }
                )
        if not acquired:
            running_operation = CRM_SYNC_LOCK_STATE.get("operation") or "CRM 同步"
            detail = f"{CRM_SYNC_LOCK_MESSAGE} 当前任务：{running_operation}"
            if age_seconds is not None:
                detail += f"，已运行约 {age_seconds} 秒"
            raise CrmSyncBusyError(detail)
    else:
        CRM_SYNC_LOCK_STATE.clear()
        CRM_SYNC_LOCK_STATE.update(
            {
                "operation": operation,
                "acquired_at": time.time(),
                "lease_seconds": lease_seconds,
                "stale_lock_recovered": False,
            }
        )
    try:
        yield CRM_SYNC_LOCK_STATE.copy()
    finally:
        if acquired:
            CRM_SYNC_LOCK_STATE.clear()
            try:
                CRM_SYNC_LOCK.release()
            except RuntimeError:
                pass


@contextmanager
def crm_detail_lock(order_key: str, timeout_seconds: int = 300):
    """单订单详情同步锁，基于 order_key 隔离，不同订单可以并行。

    使用信号量限制最大并发数，避免 CDP 浏览器压力过大。
    """
    with CRM_DETAIL_LOCK:
        if order_key not in _detail_locks:
            _detail_locks[order_key] = threading.Lock()
        lock = _detail_locks[order_key]

    acquired_sem = _detail_semaphore.acquire(timeout=timeout_seconds)
    if not acquired_sem:
        raise CrmSyncBusyError(f"当前进行中的 CRM 详情同步已达上限（{MAX_CONCURRENT_DETAIL_SYNCS}），请稍后重试。")

    acquired_lock = lock.acquire(blocking=False)
    if not acquired_lock:
        _detail_semaphore.release()
        raise CrmSyncBusyError(f"该订单正在进行 CRM 详情同步，请稍后重试。订单标识：{order_key}")

    try:
        yield
    finally:
        lock.release()
        _detail_semaphore.release()
        # 清理长期未使用的锁，避免内存泄漏
        with CRM_DETAIL_LOCK:
            # 尝试获取锁判断是否空闲
            if lock.acquire(blocking=False):
                lock.release()
                _detail_locks.pop(order_key, None)


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    if row.is_secret:
        return decrypt_value(str(row.value))
    return str(row.value)


def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def config_int(session: Session, key: str, default: int) -> int:
    try:
        return int(config_value(session, key, str(default)))
    except (TypeError, ValueError):
        return default


def ensure_request_file(
    *,
    configured_path: str,
    request_json: str,
    fallback_prefix: str,
) -> tuple[str, Path | None]:
    path_text = str(configured_path or "").strip()
    json_text = str(request_json or "").strip()
    if path_text and Path(path_text).exists():
        return path_text, None
    if not json_text:
        return path_text, None
    target = Path(path_text) if path_text else Path("/private/tmp") / f"{fallback_prefix}-{hashlib.sha1(json_text.encode()).hexdigest()[:12]}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json_text, encoding="utf-8")
    return str(target), target


def _remaining_login_cooldown_seconds() -> int:
    try:
        state = json.loads(FXIAOKE_LOGIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    last_failure_at = float(state.get("last_failure_at") or 0) / 1000
    if last_failure_at <= 0:
        return 0
    remaining = FXIAOKE_LOGIN_COOLDOWN_SECONDS - (time.time() - last_failure_at)
    return max(0, int(remaining))


def _cdp_http_get(cdp_url: str, path: str, timeout: int = 5) -> Any:
    """向 CDP 浏览器发起 HTTP GET 请求，返回 JSON 解析结果。"""
    base_url = cdp_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _cdp_list_pages(cdp_url: str) -> list[dict[str, Any]]:
    """获取 CDP 浏览器的所有页面列表。"""
    result = _cdp_http_get(cdp_url, "/json/list", timeout=3)
    if isinstance(result, list):
        return result
    return []


def _cdp_close_page(cdp_url: str, page_id: str) -> bool:
    """关闭 CDP 浏览器的一个页面。"""
    result = _cdp_http_get(cdp_url, f"/json/close/{page_id}", timeout=3)
    return result is not None


def _cdp_page_is_login(page: dict[str, Any]) -> bool:
    """判断页面是否停留在登录页。"""
    return "login" in str(page.get("url") or "").lower() or "CRM登录系统" in str(page.get("title") or "")


def _cdp_page_is_crm_home(page: dict[str, Any]) -> bool:
    """判断页面是否在 CRM 首页（已登录状态）。"""
    url = str(page.get("url") or "").lower()
    return "fxiaoke.com" in url and "/xv/ui/home" in url


def _cdp_page_is_sales_order(page: dict[str, Any]) -> bool:
    """判断页面是否在销售订单列表。"""
    return "salesorderobj" in str(page.get("url") or "").lower()


def _cdp_cleanup_extra_pages(cdp_url: str) -> dict[str, Any]:
    """清理 CDP 浏览器多余页签，只保留一个最佳的页签。

    优先级：销售订单列表 > CRM 首页 > 第一个非登录页 > 第一个页
    返回清理后的状态。
    """
    pages = _cdp_list_pages(cdp_url)
    if not pages:
        return {"pages": [], "kept": None, "closed": 0, "message": "no pages found"}

    # 按优先级排序：销售订单 > CRM首页 > 非登录页 > 其他
    priority = lambda p: (
        0 if _cdp_page_is_sales_order(p) else
        1 if _cdp_page_is_crm_home(p) else
        2 if not _cdp_page_is_login(p) else
        3
    )
    sorted_pages = sorted(pages, key=priority)
    best_page = sorted_pages[0]

    closed = 0
    for page in pages:
        if page.get("id") != best_page.get("id"):
            if _cdp_close_page(cdp_url, page.get("id", "")):
                closed += 1

    remaining = _cdp_list_pages(cdp_url)
    return {
        "pages": remaining,
        "kept": {"id": best_page.get("id"), "url": best_page.get("url"), "title": best_page.get("title")},
        "closed": closed,
        "remaining_count": len(remaining),
        "is_login_page": _cdp_page_is_login(best_page),
    }


def crm_external_browser_pids(port: int, user_data_dir: str) -> list[int]:
    patterns = [
        f"remote-debugging-port={port}",
        f"user-data-dir={user_data_dir}",
        f"fxiaoke_start_cdp_chrome.mjs --port={port}",
    ]
    pids: set[int] = set()
    for pattern in patterns:
        try:
            output = subprocess.check_output(["pgrep", "-f", pattern], text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            continue
        for line in output.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != os.getpid():
                pids.add(pid)
    return sorted(pids)


def ensure_crm_browser_headless(session: Session) -> None:
    port = config_int(session, "crm_cdp_port", 9334)
    user_data_dir = config_value(session, "crm_cdp_user_data_dir", f"/private/tmp/fxiaoke-cdp-profile-{port}").strip()
    
    version = {}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            version = json.loads(response.read().decode("utf-8"))
    except Exception:
        pass
        
    is_running = bool(version)
    is_headless = "HeadlessChrome" in str(version.get("User-Agent") or "")
    
    if is_running and is_headless:
        return
        
    pids = crm_external_browser_pids(port, user_data_dir)
    for pid in pids:
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        time.sleep(0.5)
        for pid in pids:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
                
    chrome_bin = config_value(session, "crm_chrome_bin", "").strip()
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_start_cdp_chrome.mjs"
    if not script_path.exists():
        raise RuntimeError(f"CRM 专用浏览器启动脚本不存在：{script_path}")
        
    command = [
        "node",
        str(script_path),
        f"--port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    
    env = os.environ.copy()
    if chrome_bin:
        env["CHROME_BIN"] = chrome_bin
        
    try:
        subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[3]),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(2.0)
    except Exception as exc:
        raise RuntimeError(f"自动启动无头 CRM 浏览器失败：{exc}") from exc


def preflight_crm_cdp_browser(cdp_url: str, *, allow_login_page: bool = False) -> dict[str, Any]:
    base_url = cdp_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/json/list", timeout=3) as response:
            pages = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CRM 专用浏览器不可连接：{cdp_url}；请先在系统接入页启动 CRM 专用浏览器。原始错误：{exc}") from exc
    if not isinstance(pages, list):
        raise RuntimeError(f"CRM 专用浏览器返回异常：{cdp_url}/json/list")

    login_pages = [page for page in pages if _cdp_page_is_login(page)]
    logged_in_pages = [
        page for page in pages
        if "fxiaoke.com" in str(page.get("url") or "").lower()
        and "/XV/UI/Home" in str(page.get("url") or "")
        and not _cdp_page_is_login(page)
    ]
    login_page_blocked = bool(login_pages and not logged_in_pages)
    has_multiple_pages = len(pages) > 1

    # 多页签清理
    cleanup_result: dict[str, Any] = {}
    if has_multiple_pages:
        cleanup_result = _cdp_cleanup_extra_pages(cdp_url)
        # 清理后重新检查登录状态
        if cleanup_result.get("is_login_page"):
            login_pages = [{"url": cleanup_result["kept"].get("url"), "title": cleanup_result["kept"].get("title")}]
            logged_in_pages = []
            login_page_blocked = True
        else:
            login_pages = []
            logged_in_pages = [{"url": cleanup_result["kept"].get("url"), "title": cleanup_result["kept"].get("title")}]
            login_page_blocked = False

    # 仅当浏览器停留在登录页且自动登录也被风控阻止时才要求人工登录
    if login_page_blocked:
        remaining = _remaining_login_cooldown_seconds()
        if remaining > 0:
            raise RuntimeError(
                f"CRM 自动登录仍在风控冷却期，约 {remaining} 秒后才会重试。"
                "如需立即使用，请点击「人工登录模式」手动完成登录。"
            )
        # 有凭据时允许 Node.js 尝试自动登录；无凭据时也给机会让 Node.js 报出更明确的信息
        if not allow_login_page:
            # 无凭据配置，但浏览器在登录页——让 Node.js 尝试，不行会报"未配置 CRM 账号密码"
            pass

    return {
        "cdp_url": cdp_url,
        "page_count": len(pages),
        "login_page_count": len(login_pages),
        "logged_in_page_count": len(logged_in_pages),
        "login_page_blocked": login_page_blocked,
        "login_page_allowed_for_auto_login": bool(login_page_blocked and allow_login_page),
        "cleanup": cleanup_result,
        "titles": [str(page.get("title") or "")[:80] for page in pages[:5]],
    }


def crm_replay_error_message(completed: subprocess.CompletedProcess[str], fallback: str) -> str:
    text = (completed.stderr or completed.stdout or fallback).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = str(parsed.get("error") or "").strip()
    if error:
        return error
    return text


def _is_transient_subprocess_error(error_message: str) -> bool:
    """判断是否为可重试的瞬态错误（网络抖动、浏览器暂时不可用等）。"""
    transient_markers = [
        "connection refused",
        "connection reset",
        "timeout",
        "timed out",
        "CDP websocket connection failed",
        "CDP command timed out",
        "Target closed",
        "Session closed",
        "Browser has disconnected",
        "net::ERR_",
        "net::ERR_TIMED_OUT",
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "ENOTFOUND",
        "EAI_AGAIN",
        "socket hang up",
        "fetch failed",
        "NetworkError",
        "Network Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
    ]
    lower = error_message.lower()
    return any(marker.lower() in lower for marker in transient_markers)


def _run_replay_subprocess_with_retry(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> subprocess.CompletedProcess[str]:
    """带指数退避重试的 subprocess 执行，仅对瞬态错误重试。"""
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if completed.returncode == 0:
                return completed
            error_message = crm_replay_error_message(completed, "CRM 同步脚本执行失败")
            if attempt < max_retries and _is_transient_subprocess_error(error_message):
                delay = base_delay * (2 ** attempt)
                last_error = error_message
                time.sleep(delay)
                continue
            raise RuntimeError(error_message)
        except subprocess.TimeoutExpired as exc:
            error_message = f"CRM 同步脚本执行超时 ({timeout}s)"
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                last_error = error_message
                time.sleep(delay)
                continue
            raise RuntimeError(error_message) from exc
    raise RuntimeError(f"CRM 同步脚本重试 {max_retries} 次后仍失败: {last_error}")


def update_crm_sync_stage(session: Session, sync_run: CrmSyncRun, stage: str, detail: dict[str, Any] | None = None) -> None:
    payload = loads(sync_run.detail_json, {})
    history = payload.get("stage_history")
    if not isinstance(history, list):
        history = []
    history.append({"stage": stage, "at": now_utc().isoformat(), **(detail or {})})
    payload["stage"] = stage
    payload["stage_at"] = now_utc().isoformat()
    payload["stage_history"] = history[-30:]
    sync_run.detail_json = dumps(payload)
    sync_run.finished_at = None
    session.add(sync_run)
    session.commit()


def crm_order_sync_due(session: Session, *, now: datetime | None = None) -> bool:
    if not config_bool(session, "crm_sync_enabled", False):
        return False
    interval = max(60, config_int(session, "crm_sync_interval_seconds", 3600))
    last_sync = config_value(session, "crm_sales_orders_last_sync_at", "").strip()
    if not last_sync:
        return True
    try:
        last = datetime.fromisoformat(last_sync)
    except ValueError:
        return True
    current = now or now_utc()
    if last.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    return (current - last).total_seconds() >= interval


def schedule_crm_order_sync_if_due(session: Session) -> dict[str, Any]:
    if not crm_order_sync_due(session):
        return {"queued": False, "reason": "not due"}
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "busy": True, "message": CRM_SYNC_LOCK_MESSAGE, "reason": "already queued", "job_id": existing.id}
    job = ProcessingJob(job_type="sync_crm_sales_orders", payload_json=dumps({"source": "auto"}), status="Pending")
    session.add(job)
    session.commit()
    return {"queued": True, "job_id": job.id}


def queue_crm_order_sync(session: Session, *, source: str = "manual") -> dict[str, Any]:
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "busy": True, "message": CRM_SYNC_LOCK_MESSAGE, "reason": "already queued", "job_id": existing.id}
    job = ProcessingJob(job_type="sync_crm_sales_orders", payload_json=dumps({"source": source}), status="Pending")
    session.add(job)
    session.commit()
    return {"queued": True, "job_id": job.id}


def recover_stale_crm_sync_runs(session: Session) -> int:
    """将卡在 Running 状态超过租约时间的 CrmSyncRun 标记为 Failed。"""
    lease_seconds = crm_sync_lock_lease_seconds(session)
    cutoff = now_utc().replace(tzinfo=None) if now_utc().tzinfo is None else now_utc()
    from datetime import timedelta as _td
    stale = (
        session.query(CrmSyncRun)
        .filter(
            CrmSyncRun.status == "Running",
            CrmSyncRun.started_at < cutoff - _td(seconds=lease_seconds),
        )
        .all()
    )
    for sync_run in stale:
        sync_run.status = "Failed"
        sync_run.finished_at = now_utc()
        sync_run.error_message = "同步超时：任务卡在 Running 状态超过租约时间，自动标记为失败"
        detail = loads(sync_run.detail_json, {})
        detail.update({"stage": "Failed", "stage_at": now_utc().isoformat(), "error_type": "StaleRunningRecovery"})
        sync_run.detail_json = dumps(detail)
        session.add(
            AuditEvent(
                event_type="CrmSyncRunStaleRecovered",
                related_object_type="CrmSyncRun",
                related_object_id=sync_run.id,
                detail=dumps({"message": "Stale Running CrmSyncRun recovered", "started_at": sync_run.started_at.isoformat() if sync_run.started_at else None}),
            )
        )
    if stale:
        session.commit()
    return len(stale)


def run_crm_sales_order_sync(session: Session, *, trigger: str = "manual") -> dict[str, Any]:
    recover_stale_crm_sync_runs(session)
    with crm_sync_lock(session, "CRM 销售订单同步") as lock_state:
        if lock_state.get("stale_lock_recovered"):
            session.add(
                AuditEvent(
                    event_type="CrmSyncStaleLockRecovered",
                    related_object_type="CrmSyncRun",
                    related_object_id="",
                    detail=dumps({"message": CRM_SYNC_LOCK_STALE_MESSAGE, "operation": "CRM 销售订单同步"}),
                )
            )
            session.commit()
        return _run_crm_sales_order_sync(session, trigger=trigger)


def _run_crm_sales_order_sync(session: Session, *, trigger: str = "manual") -> dict[str, Any]:
    sync_run = CrmSyncRun(source_system=DEFAULT_SOURCE_SYSTEM, sync_type="sales_orders", status="Running", trigger=trigger)
    session.add(sync_run)
    session.commit()
    # 在 try 之前缓存 id，避免 rollback 后 detached 对象取不到 id
    sync_run_id: str | None = sync_run.id

    try:
        update_crm_sync_stage(session, sync_run, "ReplayRunning", {"message": "正在连接 CRM 专用浏览器并拉取销售订单"})
        rows, command_summary = fetch_sales_orders_via_replay(session)
        sync_run = session.get(CrmSyncRun, sync_run.id) or sync_run
        update_crm_sync_stage(session, sync_run, "Upserting", {"source_total": len(rows), "pages": command_summary.get("pages", [])})
        result = upsert_crm_sales_orders(session, rows)
        sync_run = session.get(CrmSyncRun, sync_run.id) or sync_run
        sync_run.status = "Completed"
        sync_run.finished_at = now_utc()
        sync_run.created_count = result["created"]
        sync_run.updated_count = result["updated"]
        sync_run.unchanged_count = result["unchanged"]
        sync_run.total_count = result["total"]
        detail = loads(sync_run.detail_json, {})
        detail.update({"stage": "Completed", "stage_at": now_utc().isoformat(), "command": command_summary, "source_total": len(rows)})
        sync_run.detail_json = dumps(detail)
        set_config(session, "crm_sales_orders_last_sync_at", now_utc().isoformat(), is_secret=False)
        session.commit()
        return {"ok": True, "sync_run_id": sync_run.id, **result, "command": command_summary}
    except Exception as exc:
        session.rollback()
        # 快速保存失败状态，避免 CrmSyncRun 永久卡在 Running
        if not _save_sync_run_failure(session, sync_run_id, trigger, exc, retries=3):
            logger.error("CRM 同步异常：无法保存 sync_run 失败状态", exc_info=True)
        raise


def _save_sync_run_failure(session: Session, sync_run_id: str | None, trigger: str, exc: Exception, retries: int = 3) -> bool:
    """多次尝试保存 sync_run 失败状态，避免 rollback 后的 detached 问题。"""
    for attempt in range(retries):
        try:
            sync_run = session.get(CrmSyncRun, sync_run_id) if sync_run_id else None
            if sync_run is None:
                sync_run = CrmSyncRun(source_system=DEFAULT_SOURCE_SYSTEM, sync_type="sales_orders", status="Failed", trigger=trigger)
                session.add(sync_run)
            sync_run.status = "Failed"
            sync_run.finished_at = now_utc()
            sync_run.error_message = str(exc)
            detail = loads(sync_run.detail_json, {})
            detail.update({"stage": "Failed", "stage_at": now_utc().isoformat(), "error_type": exc.__class__.__name__})
            sync_run.detail_json = dumps(detail)
            session.commit()
            return True
        except Exception:
            session.rollback()
            if attempt < retries - 1:
                time.sleep(0.5)
    # 最后的兜底：尝试极简写入
    try:
        sync_run = session.get(CrmSyncRun, sync_run_id) if sync_run_id else None
        if sync_run is not None:
            sync_run.status = "Failed"
            sync_run.finished_at = now_utc()
            sync_run.error_message = str(exc)[:500]
            session.commit()
            return True
    except Exception:
        session.rollback()
    return False


def crm_order_single_row(order: CrmSalesOrder) -> dict[str, Any]:
    return {
        "crm_order_id": order.crm_order_id,
        "crm_order_no": order.crm_order_no,
        "customer_id": order.customer_id,
        "customer_name": order.customer_name,
        "opportunity_id": order.opportunity_id,
        "opportunity_name": order.opportunity_name,
        "life_status": order.life_status,
        "approval_status": order.approval_status,
        "order_date": order.order_date,
        "settlement_method": order.settlement_method,
        "order_amount": order.order_amount,
        "received_amount": order.received_amount,
        "receivable_amount": order.receivable_amount,
        "invoice_amount": order.invoice_amount,
        "product_amount": order.product_amount,
        "logistics_status": order.logistics_status,
        "shipment_status": order.shipment_status,
        "invoice_status": order.invoice_status,
        "sales_user_id": order.sales_user_id,
        "sales_user_name": order.sales_user_name,
        "owner_department": order.owner_department,
        "receipt_contact": order.receipt_contact,
        "receipt_phone": order.receipt_phone,
        "receipt_address": order.receipt_address,
        "delivery_date": order.delivery_date,
        "remark": order.remark,
        "attachment_files": "; ".join(str(item) for item in loads(order.attachment_files_json, []) if str(item).strip()),
        "attachments": loads(order.raw_json, {}).get("attachments") or [],
    }


def fetch_single_order_detail_via_replay(session: Session, order: CrmSalesOrder) -> tuple[dict[str, Any], dict[str, Any]]:
    """通过 CDP 浏览器为单个订单拉取详情页。

    也支持传入带有 crm_order_no 的字典（用于强制同步尚未入库的订单）。
    """
    detail_request_path = config_value(session, "crm_fxiaoke_detail_request_file", "").strip()
    detail_request_json = config_value(session, "crm_fxiaoke_detail_request_json", "").strip()
    if not detail_request_path and not detail_request_json:
        raise RuntimeError("请先配置 crm_fxiaoke_detail_request_file 或 crm_fxiaoke_detail_request_json")
    cdp_url = config_value(session, "crm_cdp_url", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_replay_sales_orders.mjs"
    if not script_path.exists():
        raise RuntimeError(f"CRM 同步脚本不存在：{script_path}")
    ensure_crm_browser_headless(session)
    cdp_preflight = preflight_crm_cdp_browser(
        cdp_url,
        allow_login_page=bool(config_value(session, "crm_username", "").strip() and config_value(session, "crm_password", "").strip()),
    )

    if isinstance(order, CrmSalesOrder):
        row_dict = crm_order_single_row(order)
        row_id = str(order.id)
    elif isinstance(order, dict):
        row_dict = dict(order)
        row_id = hashlib.md5(str(row_dict.get("crm_order_no") or "").encode()).hexdigest()[:12]
    else:
        raise RuntimeError("order 必须是 CrmSalesOrder 或 dict")

    single_row_path = Path("/private/tmp") / f"fxiaoke-single-row-{row_id}.json"
    temp_detail_request_path: Path | None = None
    try:
        single_row_path.write_text(json.dumps(row_dict, ensure_ascii=False), encoding="utf-8")
        detail_request_path, temp_detail_request_path = ensure_request_file(
            configured_path=detail_request_path,
            request_json=detail_request_json,
            fallback_prefix="fxiaoke-detail-request",
        )
        command = [node_bin, str(script_path), f"--single-row={single_row_path}", f"--detail-request={detail_request_path}"]
        env = {
            "FXIAOKE_CDP_URL": cdp_url,
            "FXIAOKE_PAGE_SIZE": "1",
            "FXIAOKE_DETAIL_ENABLED": "true",
            "FXIAOKE_USERNAME": config_value(session, "crm_username", "").strip(),
            "FXIAOKE_PASSWORD": config_value(session, "crm_password", "").strip(),
        }
        timeout = max(30, config_int(session, "crm_sync_timeout_seconds", 120))
        max_retries = max(0, config_int(session, "crm_sync_max_retries", 3))
        completed = _run_replay_subprocess_with_retry(
            command,
            cwd=str(Path(__file__).resolve().parents[3]),
            env={**os.environ, **env},
            timeout=timeout,
            max_retries=max_retries,
            base_delay=2.0,
        )
        output = json.loads(completed.stdout)
        json_path = output.get("jsonPath")
        if not json_path:
            raise RuntimeError("CRM 单条详情同步脚本未返回 jsonPath")
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        if not rows:
            raise RuntimeError("CRM 单条详情同步未返回订单详情")
        row = rows[0]
        if str(row.get("detail_sync_status") or "").lower() == "failed":
            raise RuntimeError(str(row.get("detail_sync_error") or "CRM 单条详情同步失败"))
        failed_detail = next((item for item in output.get("detailPages", []) if item.get("status") == "Failed"), None)
        if failed_detail is not None:
            raise RuntimeError(str(failed_detail.get("error") or "CRM 单条详情同步失败"))
        return row, {"cdp_url": cdp_url, "cdp_preflight": cdp_preflight, "detail_request_file": detail_request_path, "json_path": json_path, "detail_pages": output.get("detailPages", [])}
    finally:
        try:
            single_row_path.unlink()
        except FileNotFoundError:
            pass
        if temp_detail_request_path is not None:
            try:
                temp_detail_request_path.unlink()
            except FileNotFoundError:
                pass


def retry_crm_order_detail_sync(session: Session, order: CrmSalesOrder, *, acquire_lock: bool = True) -> dict[str, Any]:
    if acquire_lock:
        order_key = f"{order.source_system or DEFAULT_SOURCE_SYSTEM}:{order.crm_order_no or order.id}"
        with crm_detail_lock(order_key):
            return retry_crm_order_detail_sync(session, order, acquire_lock=False)
    try:
        row, command = fetch_single_order_detail_via_replay(session, order)
        result = upsert_crm_sales_orders(session, [row])
        refreshed = session.get(CrmSalesOrder, order.id) or order
        session.add(
            AuditEvent(
                event_type="CrmOrderDetailRetrySucceeded",
                related_object_type="CrmSalesOrder",
                related_object_id=refreshed.id,
                detail=dumps({"crm_order_id": refreshed.crm_order_id, "crm_order_no": refreshed.crm_order_no, "command": command, "result": result}),
            )
        )
        return {"ok": True, "order_id": refreshed.id, "result": result, "command": command}
    except Exception as exc:
        raw = loads(order.raw_json, {})
        raw["detail_sync_status"] = "Failed"
        raw["detail_sync_error"] = str(exc)
        order.raw_json = dumps(raw)
        order.sync_status = "DetailFailed"
        order.updated_at = now_utc()
        session.add(order)
        session.add(
            AuditEvent(
                event_type="CrmOrderDetailRetryFailed",
                related_object_type="CrmSalesOrder",
                related_object_id=order.id,
                detail=dumps({"crm_order_id": order.crm_order_id, "crm_order_no": order.crm_order_no, "error": str(exc)}),
            )
        )
        session.commit()
        raise


def force_sync_crm_order_by_no(session: Session, crm_order_no: str) -> dict[str, Any]:
    with crm_sync_lock(session, f"CRM 指定订单强制同步：{crm_order_no}") as lock_state:
        if lock_state.get("stale_lock_recovered"):
            session.add(
                AuditEvent(
                    event_type="CrmSyncStaleLockRecovered",
                    related_object_type="CrmSalesOrder",
                    related_object_id="",
                    detail=dumps({"message": CRM_SYNC_LOCK_STALE_MESSAGE, "operation": "CRM 指定订单强制同步", "crm_order_no": crm_order_no}),
                )
            )
            session.commit()
        return _force_sync_crm_order_by_no(session, crm_order_no)


def _force_sync_crm_order_by_no(session: Session, crm_order_no: str) -> dict[str, Any]:
    order_no = str(crm_order_no or "").strip()
    if not order_no:
        raise RuntimeError("CRM 订单号不能为空")
    order = session.query(CrmSalesOrder).filter(CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM, CrmSalesOrder.crm_order_no == order_no).first()
    list_command = None
    if order is None:
        # 优先通过详情接口直接拉取（比全量列表拉取更高效）
        try:
            synthetic_row = {"crm_order_no": order_no, "crm_order_id": order_no}
            row, detail_command = fetch_single_order_detail_via_replay(session, synthetic_row)
            result = upsert_crm_sales_orders(session, [row])
            session.flush()
            order = session.query(CrmSalesOrder).filter(
                CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM,
                CrmSalesOrder.crm_order_no == order_no,
            ).first()
            if order is None:
                raise RuntimeError(f"CRM 订单入库失败：{order_no}")
            list_command = detail_command
        except Exception as detail_error:
            # 详情直接拉取失败，回退到全量列表拉取
            rows, command = fetch_sales_orders_via_replay(session)
            matched = [row for row in rows if str(row.get("crm_order_no") or "").strip() == order_no]
            if not matched:
                raise RuntimeError(f"CRM 列表同步未找到订单：{order_no}（详情拉取也失败：{detail_error}）") from detail_error
            upsert_crm_sales_orders(session, matched)
            session.flush()
            order = session.query(CrmSalesOrder).filter(
                CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM,
                CrmSalesOrder.crm_order_no == order_no,
            ).first()
            if order is None:
                raise RuntimeError(f"CRM 订单入库失败：{order_no}")
            list_command = command

    detail_retry = retry_crm_order_detail_sync(session, order, acquire_lock=False)
    session.flush()
    refreshed = session.query(CrmSalesOrder).filter(CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM, CrmSalesOrder.crm_order_no == order_no).first() or order
    from backend.app.services.order_middle_platform import crm_order_parsed_event, process_crm_order_parsed_event

    event = crm_order_parsed_event(refreshed, trace_id=f"force-sync-{order_no}")
    event["force_revalidate"] = True
    process_result = process_crm_order_parsed_event(session, event)
    session.add(
        AuditEvent(
            event_type="CrmOrderForceSynced",
            related_object_type="CrmSalesOrder",
            related_object_id=refreshed.id,
            detail=dumps({"crm_order_no": order_no, "detail_retry": detail_retry, "process_result": process_result, "list_command": list_command}),
        )
    )
    return {"ok": True, "crm_order_id": refreshed.id, "crm_order_no": order_no, "detail_retry": detail_retry, "process_result": process_result}


def run_crm_integration_test(session: Session) -> dict[str, Any]:
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    timeout_seconds = max(30, min(600, config_int(session, "crm_sync_timeout_seconds", 120)))
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_integration_smoke.mjs"
    if not script_path.exists():
        raise RuntimeError(f"CRM 接入测试脚本不存在：{script_path}")

    completed = subprocess.run(
        [node_bin, str(script_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or "CRM 接入测试执行失败")
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CRM 接入测试输出不是 JSON：{stdout[:500]}") from exc
    output["command"] = {
        "script": str(script_path),
        "timeout_seconds": timeout_seconds,
        "stderr": stderr,
    }
    return output


def fetch_sales_orders_via_replay(session: Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    request_path = config_value(session, "crm_fxiaoke_request_file", "").strip()
    request_json = config_value(session, "crm_fxiaoke_request_json", "").strip()
    detail_request_path = config_value(session, "crm_fxiaoke_detail_request_file", "").strip()
    detail_request_json = config_value(session, "crm_fxiaoke_detail_request_json", "").strip()
    cdp_url = config_value(session, "crm_cdp_url", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    page_size = str(max(1, config_int(session, "crm_sync_page_size", 20)))
    max_pages = str(max(0, config_int(session, "crm_sync_max_pages", 0)))
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_replay_sales_orders.mjs"

    if not script_path.exists():
        raise RuntimeError(f"CRM 同步脚本不存在：{script_path}")
    if not request_path and not request_json:
        raise RuntimeError("请先配置 crm_fxiaoke_request_file 或 crm_fxiaoke_request_json")
    ensure_crm_browser_headless(session)
    cdp_preflight = preflight_crm_cdp_browser(
        cdp_url,
        allow_login_page=bool(config_value(session, "crm_username", "").strip() and config_value(session, "crm_password", "").strip()),
    )

    temp_request_path: Path | None = None
    temp_detail_request_path: Path | None = None
    try:
        request_path, temp_request_path = ensure_request_file(
            configured_path=request_path,
            request_json=request_json,
            fallback_prefix="fxiaoke-list-request",
        )
        detail_request_path, temp_detail_request_path = ensure_request_file(
            configured_path=detail_request_path,
            request_json=detail_request_json,
            fallback_prefix="fxiaoke-detail-request",
        )

        command = [node_bin, str(script_path), f"--request={request_path}"]
        if detail_request_path:
            command.append(f"--detail-request={detail_request_path}")
        env = {
            "FXIAOKE_CDP_URL": cdp_url,
            "FXIAOKE_PAGE_SIZE": page_size,
            "FXIAOKE_MAX_PAGES": max_pages,
            "FXIAOKE_MIN_ORDER_DATE": config_value(session, "crm_sync_min_order_date", "").strip(),
            "FXIAOKE_DETAIL_ENABLED": "true" if config_bool(session, "crm_sync_detail_enabled", True) else "false",
            "FXIAOKE_REQUEST_TIMEOUT_MS": str(max(1000, config_int(session, "crm_sync_request_timeout_ms", 15000))),
            "FXIAOKE_USERNAME": config_value(session, "crm_username", "").strip(),
            "FXIAOKE_PASSWORD": config_value(session, "crm_password", "").strip(),
            "FXIAOKE_DETAIL_CONCURRENCY": str(max(1, config_int(session, "crm_sync_detail_concurrency", 3))),
            "FXIAOKE_REQUEST_RETRY_MAX": str(max(0, config_int(session, "crm_sync_max_retries", 3))),
        }
        timeout = max(30, config_int(session, "crm_sync_timeout_seconds", 120))
        max_retries = max(0, config_int(session, "crm_sync_max_retries", 3))
        completed = _run_replay_subprocess_with_retry(
            command,
            cwd=str(Path(__file__).resolve().parents[3]),
            env={**os.environ, **env},
            timeout=timeout,
            max_retries=max_retries,
            base_delay=2.0,
        )
        output = json.loads(completed.stdout)
        json_path = output.get("jsonPath")
        if not json_path:
            raise RuntimeError("CRM 同步脚本未返回 jsonPath")
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        if not isinstance(rows, list):
            raise RuntimeError("CRM 同步脚本返回 rows 格式错误")
        return rows, {
            "cdp_url": cdp_url,
            "cdp_preflight": cdp_preflight,
            "request_file": request_path,
            "detail_request_file": detail_request_path,
            "json_path": json_path,
            "csv_path": output.get("csvPath"),
            "pages": output.get("pages", []),
            "detail_pages": output.get("detailPages", []),
        }
    finally:
        if temp_request_path is not None:
            try:
                temp_request_path.unlink()
            except FileNotFoundError:
                pass
        if temp_detail_request_path is not None:
            try:
                temp_detail_request_path.unlink()
            except FileNotFoundError:
                pass


def payload_hash(row: dict[str, Any]) -> str:
    stable = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def protect_degraded_detail_row(session: Session, existing: CrmSalesOrder | None, row: dict[str, Any]) -> dict[str, Any]:
    """Avoid replacing a richer CRM detail snapshot with an empty DOM fallback."""
    if existing is None or not existing.raw_json:
        return row
    existing_raw = loads(existing.raw_json, {})
    if not isinstance(existing_raw, dict):
        return row
    protected = dict(row)
    raw_sources = [existing_raw]
    snapshots = (
        session.query(CrmOrderSnapshot)
        .filter(
            CrmOrderSnapshot.source_system == existing.source_system,
            CrmOrderSnapshot.crm_order_id == existing.crm_order_id,
        )
        .order_by(CrmOrderSnapshot.version.desc())
        .limit(10)
        .all()
    )
    for snapshot in snapshots:
        snapshot_raw = loads(snapshot.raw_json, {})
        if isinstance(snapshot_raw, dict):
            raw_sources.append(snapshot_raw)

    def first_raw_value(key: str) -> Any:
        for source in raw_sources:
            value = source.get(key)
            if isinstance(value, list):
                if value:
                    return value
            elif str(value or "").strip():
                return value
        return None

    incoming_order_items = protected.get("order_items") or protected.get("items")
    if isinstance(incoming_order_items, list) and incoming_order_items:
        protected["order_items"] = incoming_order_items
        protected["items"] = incoming_order_items
    previous_items = first_raw_value("order_items") or first_raw_value("items")
    if not (isinstance(incoming_order_items, list) and incoming_order_items) and isinstance(previous_items, list) and previous_items:
        for key in ["order_items", "items"]:
            incoming = protected.get(key)
            if not isinstance(incoming, list) or not incoming:
                protected[key] = previous_items
    scalar_keys = [
        "customer_name",
        "opportunity_name",
        "life_status",
        "approval_status",
        "order_date",
        "settlement_method",
        "order_amount",
        "received_amount",
        "receivable_amount",
        "invoice_amount",
        "product_amount",
        "sales_user_name",
        "sales_user_email",
        "owner_department",
    ]
    for key in scalar_keys:
        if str(protected.get(key) or "").strip():
            continue
        previous = first_raw_value(key)
        if str(previous or "").strip():
            protected[key] = previous
            continue
        current = getattr(existing, key, None)
        if str(current or "").strip():
            protected[key] = current
    for key in ["attachments"]:
        incoming = protected.get(key)
        previous = first_raw_value(key)
        if (not isinstance(incoming, list) or not incoming) and isinstance(previous, list) and previous:
            protected[key] = previous
    if not str(protected.get("attachment_files") or "").strip():
        previous_attachment_files = first_raw_value("attachment_files")
        if str(previous_attachment_files or "").strip():
            protected["attachment_files"] = previous_attachment_files
    return protected


def normalized_text(value: Any) -> str:
    return str(value or "").strip()


def normalized_lower(value: Any) -> str:
    return normalized_text(value).lower()


def config_json(session: Session, key: str, default: Any) -> Any:
    try:
        return loads(config_value(session, key, ""), default)
    except Exception:
        return default


def normalize_master_name(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def first_non_empty(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def clear_product_sku_master(session: Session) -> dict[str, int]:
    counts = {
        "promotion_rules": session.query(PromotionRule).count(),
        "channel_pricings": session.query(ChannelPricing).count(),
        "inventory_snapshots": session.query(ProductInventorySnapshot).count(),
        "skus": session.query(ProductSKU).count(),
        "spus": session.query(ProductSPU).count(),
    }
    session.query(PromotionRule).delete(synchronize_session=False)
    session.query(ChannelPricing).delete(synchronize_session=False)
    session.query(ProductInventorySnapshot).delete(synchronize_session=False)
    session.query(ProductSKU).delete(synchronize_session=False)
    session.query(ProductSPU).delete(synchronize_session=False)
    session.add(AuditEvent(event_type="ProductSkuMasterCleared", related_object_type="SystemConfig", related_object_id="master-data", detail=dumps(counts)))
    return counts


def crm_product_row_to_sku(row: dict[str, Any], index: int) -> dict[str, str]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
    product_name = first_non_empty(row, "product_name", "name", "商品名称", "产品名称") or first_non_empty(raw, "product_name", "name", "商品名称", "产品名称")
    sku_code = first_non_empty(row, "sku_code", "sku_id", "product_code", "商品编码", "产品编码", "code") or first_non_empty(raw, "sku_code", "sku_id", "product_code", "商品编码", "产品编码", "code")
    crm_product_id = first_non_empty(row, "crm_product_id", "product_id", "id", "商品ID", "产品ID") or first_non_empty(raw, "crm_product_id", "product_id", "id")
    if not sku_code:
        sku_code = crm_product_id or f"CRM-PRODUCT-{index + 1:06d}"
    if not product_name:
        product_name = sku_code
    return {
        "spu_id": crm_product_id or sku_code,
        "sku_id": sku_code,
        "name": product_name,
        "model": first_non_empty(row, "model", "specification", "规格", "型号") or first_non_empty(raw, "model", "specification", "规格", "型号"),
        "category": first_non_empty(row, "category", "商品分类", "产品分类") or first_non_empty(raw, "category", "商品分类", "产品分类") or "成品",
        "raw": dumps(row),
    }


def sync_crm_products_as_skus(session: Session, rows: list[dict[str, Any]], *, clear_existing: bool = True) -> dict[str, int]:
    if clear_existing:
        clear_product_sku_master(session)
    created_spus = 0
    created_skus = 0
    updated_skus = 0
    for index, row in enumerate(rows):
        normalized = crm_product_row_to_sku(row, index)
        spu = session.query(ProductSPU).filter_by(spu_id=normalized["spu_id"]).one_or_none()
        if spu is None:
            spu = ProductSPU(spu_id=normalized["spu_id"], name=normalized["name"], category=normalized["category"], status="Active")
            session.add(spu)
            session.flush()
            created_spus += 1
        else:
            spu.name = normalized["name"]
            spu.category = normalized["category"] or spu.category
            spu.status = "Active"
            spu.updated_at = now_utc()
        extended = loads(spu.extended_info_json, {})
        extended["crm"] = {"source": DEFAULT_SOURCE_SYSTEM, "raw": row, "synced_at": now_utc().isoformat()}
        spu.extended_info_json = dumps(extended)
        sku = session.query(ProductSKU).filter_by(sku_id=normalized["sku_id"]).one_or_none()
        if sku is None:
            sku = ProductSKU(
                spu_uuid=spu.id,
                sku_id=normalized["sku_id"],
                model=normalized["model"] or None,
                attributes_json=dumps({"source": DEFAULT_SOURCE_SYSTEM, "crm_raw": row}),
                status="Active",
            )
            session.add(sku)
            created_skus += 1
        else:
            sku.spu_uuid = spu.id
            sku.model = normalized["model"] or sku.model
            sku.attributes_json = dumps({"source": DEFAULT_SOURCE_SYSTEM, "crm_raw": row})
            sku.status = "Active"
            sku.updated_at = now_utc()
            updated_skus += 1
    session.add(AuditEvent(event_type="CrmProductSkuSynced", related_object_type="SystemConfig", related_object_id="master-data", detail=dumps({"source_total": len(rows), "created_spus": created_spus, "created_skus": created_skus, "updated_skus": updated_skus, "cleared": clear_existing})))
    return {"source_total": len(rows), "created_spus": created_spus, "created_skus": created_skus, "updated_skus": updated_skus}


def normalize_customer_row(row: dict[str, Any], source: str) -> dict[str, str]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
    name = first_non_empty(row, "customer_name", "name", "客户名称") or first_non_empty(raw, "customer_name", "name", "客户名称")
    code = first_non_empty(row, "customer_code", "code", "客户编码", "customer_id", "id") or first_non_empty(raw, "customer_code", "code", "客户编码", "customer_id", "id")
    return {"source": source, "customer_name": name, "customer_code": code}


def build_customer_mapping_from_masters(crm_rows: list[dict[str, Any]], oms_rows: list[dict[str, Any]]) -> dict[str, Any]:
    oms_by_name: dict[str, dict[str, str]] = {}
    for row in oms_rows:
        item = normalize_customer_row(row, "OMS")
        key = normalize_master_name(item["customer_name"])
        if key and item["customer_code"]:
            oms_by_name[key] = item
    mapping: dict[str, Any] = {}
    unmatched_crm: list[dict[str, str]] = []
    for row in crm_rows:
        crm = normalize_customer_row(row, "CRM")
        key = normalize_master_name(crm["customer_name"])
        if not key:
            continue
        oms = oms_by_name.get(key)
        if oms is None:
            unmatched_crm.append(crm)
            continue
        mapping[crm["customer_name"]] = {
            "customer_code": oms["customer_code"],
            "customer_name": oms["customer_name"],
            "crm_customer_code": crm["customer_code"],
            "mapping_source": "crm_oms_name_exact",
        }
    return {"mapping": mapping, "unmatched_crm": unmatched_crm, "matched_count": len(mapping)}


def sync_customer_mapping_from_masters(session: Session, crm_rows: list[dict[str, Any]], oms_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = build_customer_mapping_from_masters(crm_rows, oms_rows)
    set_config(session, "v2_customer_mapping_json", dumps(result["mapping"]), is_secret=False)
    set_config(session, "v2_customer_mapping_unmatched_json", dumps(result["unmatched_crm"]), is_secret=False)
    session.add(AuditEvent(event_type="CustomerMappingSynced", related_object_type="SystemConfig", related_object_id="customer-mapping", detail=dumps({"matched_count": result["matched_count"], "unmatched_count": len(result["unmatched_crm"])})))
    return result


def parse_order_date_value(value: Any) -> date | None:
    text = normalized_text(value)
    if not text:
        return None
    for token in ("T", " "):
        if token in text:
            text = text.split(token, 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def min_order_date_scope_result(session: Session, row: dict[str, Any]) -> tuple[bool, str | None]:
    min_date_text = config_value(session, "crm_sync_min_order_date", "").strip()
    if not min_date_text:
        min_date_text = normalized_text(config_json(session, "v2_crm_phase1_scope_json", {}).get("min_order_date"))
    if not min_date_text:
        return True, None
    min_date = parse_order_date_value(min_date_text)
    order_date = parse_order_date_value(row.get("order_date"))
    if min_date is None or order_date is None:
        return True, None
    if order_date < min_date:
        return False, f"order_date_before_crm_sync_min_order_date:{row.get('order_date')}<{min_date.isoformat()}"
    return True, None


def phase_one_scope_result(session: Session, row: dict[str, Any], existing: CrmSalesOrder | None) -> tuple[bool, str | None]:
    date_in_scope, date_ignore_reason = min_order_date_scope_result(session, row)
    if not date_in_scope:
        return False, date_ignore_reason
    if not config_bool(session, "v2_crm_phase1_scope_enabled", True):
        return True, None
    scope = config_json(session, "v2_crm_phase1_scope_json", {})
    approved_values = {str(item).strip().lower() for item in scope.get("approved_values", []) if str(item).strip()}
    approved_life_status_values = {
        str(item).strip().lower()
        for item in scope.get("approved_life_status_values", ["normal", "正常", "active"])
        if str(item).strip()
    }
    cancelled_values = {str(item).strip().lower() for item in scope.get("cancelled_values", []) if str(item).strip()}
    approval_status = normalized_lower(row.get("approval_status"))
    life_status = normalized_lower(row.get("life_status"))
    if approval_status in cancelled_values or life_status in cancelled_values:
        if existing is not None:
            return True, None
        return False, "crm_order_cancelled_before_middle_platform"
    if life_status and approved_life_status_values and life_status not in approved_life_status_values:
        return False, f"life_status_not_in_phase1_scope:{row.get('life_status')}"
    if approval_status and approved_values and approval_status not in approved_values:
        return False, f"approval_status_not_in_phase1_scope:{row.get('approval_status')}"
    list_filters = {
        "include_owner_departments": row.get("owner_department"),
        "include_settlement_methods": row.get("settlement_method"),
        "include_customer_names": row.get("customer_name"),
    }
    for key, value in list_filters.items():
        allowed = {str(item).strip() for item in scope.get(key, []) if str(item).strip()}
        if allowed and normalized_text(value) not in allowed:
            return False, f"{key}_not_in_phase1_scope:{value or ''}"
    return True, None


def upsert_crm_sales_orders(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    unchanged = 0
    ignored = 0
    row_errors = 0
    changed_orders: list[CrmSalesOrder] = []
    for row in rows:
        crm_order_id = str(row.get("crm_order_id") or "").strip()
        crm_order_no = str(row.get("crm_order_no") or "").strip()
        if not crm_order_id and not crm_order_no:
            continue
        # 使用 savepoint 隔离单行 upsert，失败只回退当前行不波及其他行
        try:
            with session.begin_nested():
                try:
                    filters = []
                    if crm_order_id:
                        filters.append(CrmSalesOrder.crm_order_id == crm_order_id)
                    if crm_order_no:
                        filters.append(CrmSalesOrder.crm_order_no == crm_order_no)
                    existing = (
                        session.query(CrmSalesOrder)
                        .filter(CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM, or_(*filters))
                        .first()
                    )
                    date_in_scope, date_ignore_reason = min_order_date_scope_result(session, row)
                    if not date_in_scope and existing is None:
                        ignored += 1
                        continue
                    row = protect_degraded_detail_row(session, existing, row)
                    digest = payload_hash(row)
                    was_new = existing is None
                    row_changed = False
                    if existing is None:
                        existing = CrmSalesOrder(
                            source_system=DEFAULT_SOURCE_SYSTEM,
                            crm_order_id=crm_order_id or crm_order_no,
                            crm_order_no=crm_order_no or crm_order_id,
                            payload_hash=digest,
                        )
                        session.add(existing)
                        created += 1
                        row_changed = True
                    elif existing.payload_hash == digest:
                        unchanged += 1
                    else:
                        updated += 1
                        row_changed = True

                    apply_order_row(existing, row, digest)
                    session.flush()
                    sync_crm_order_items(session, existing, row, digest)
                    snapshot = save_order_snapshot(session, existing, row, digest)
                    existing.latest_snapshot_id = snapshot.id
                    sync_order_attachments(session, existing, row, digest)
                    in_scope, ignore_reason = phase_one_scope_result(session, row, None if was_new else existing)
                    if in_scope:
                        existing.scope_status = "InScope"
                        existing.scope_ignore_reason = None
                        if row_changed:
                            changed_orders.append(existing)
                    else:
                        existing.scope_status = "Ignored"
                        existing.scope_ignore_reason = ignore_reason
                        existing.sync_status = "Ignored"
                        ignored += 1
                except Exception as row_exc:
                    # savepoint 内的异常会被自动还原到 savepoint；标记行错误
                    row_errors += 1
                    error_detail = {
                        "crm_order_id": crm_order_id,
                        "crm_order_no": crm_order_no,
                        "error": str(row_exc),
                        "error_type": row_exc.__class__.__name__,
                    }
                    try:
                        session.add(
                            AuditEvent(
                                event_type="CrmUpsertRowError",
                                related_object_type="CrmSalesOrder",
                                related_object_id=crm_order_id or crm_order_no,
                                detail=dumps(error_detail),
                            )
                        )
                        session.commit()
                    except Exception:
                        session.rollback()
                    raise  # 重新抛出让 begin_nested() context manager 捕获
        except Exception:
            # savepoint 已自动回滚；继续处理下一行
            pass
    session.flush()
    queued = 0
    for order in changed_orders:
        try:
            enqueue_crm_order_parsed_event(session, order)
            queued += 1
        except Exception:
            # 单个事件入队失败不影响其他订单
            session.rollback()
    return {"created": created, "updated": updated, "unchanged": unchanged, "ignored": ignored, "row_errors": row_errors, "queued_events": queued, "total": created + updated + unchanged}


def save_order_snapshot(session: Session, order: CrmSalesOrder, row: dict[str, Any], digest: str) -> CrmOrderSnapshot:
    existing = (
        session.query(CrmOrderSnapshot)
        .filter(
            CrmOrderSnapshot.source_system == order.source_system,
            CrmOrderSnapshot.crm_order_id == order.crm_order_id,
            CrmOrderSnapshot.payload_hash == digest,
        )
        .first()
    )
    previous_latest = session.query(CrmOrderSnapshot).filter(
        CrmOrderSnapshot.source_system == order.source_system,
        CrmOrderSnapshot.crm_order_id == order.crm_order_id,
        CrmOrderSnapshot.is_latest.is_(True),
    ).all()
    for snapshot in previous_latest:
        snapshot.is_latest = False
    if existing is not None:
        existing.crm_sales_order_id = order.id
        existing.crm_order_no = order.crm_order_no
        existing.raw_json = dumps(row)
        existing.parse_status = "Parsed"
        existing.is_latest = True
        existing.captured_at = now_utc()
        return existing
    latest_version = (
        session.query(func.max(CrmOrderSnapshot.version))
        .filter(
            CrmOrderSnapshot.source_system == order.source_system,
            CrmOrderSnapshot.crm_order_id == order.crm_order_id,
        )
        .scalar()
        or 0
    )
    snapshot = CrmOrderSnapshot(
        crm_sales_order_id=order.id,
        source_system=order.source_system,
        crm_order_id=order.crm_order_id,
        crm_order_no=order.crm_order_no,
        payload_hash=digest,
        version=int(latest_version) + 1,
        is_latest=True,
        parse_status="Parsed",
        raw_json=dumps(row),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def extract_attachment_records(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_attachments = row.get("attachments")
    records: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}

    def add_record(record: dict[str, Any]) -> None:
        name_key = "|".join([normalized_lower(record.get("source_file_id")), normalized_lower(record.get("file_name"))])
        if not name_key.strip("|"):
            return
        existing = by_name.get(name_key)
        if existing is None or (not existing.get("file_url") and record.get("file_url")):
            by_name[name_key] = record

    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if isinstance(item, dict):
                name = normalized_text(item.get("file_name") or item.get("name") or item.get("filename"))
                if not name:
                    continue
                raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
                add_record({
                    "file_name": name,
                    "file_url": normalized_text(
                        item.get("file_url")
                        or item.get("url")
                        or item.get("signedUrl")
                        or item.get("signed_url")
                        or item.get("download_url")
                        or item.get("downloadUrl")
                        or item.get("preview_url")
                        or item.get("previewUrl")
                        or raw.get("signedUrl")
                        or raw.get("signed_url")
                        or raw.get("download_url")
                        or raw.get("downloadUrl")
                        or raw.get("preview_url")
                        or raw.get("previewUrl")
                    )
                    or None,
                    "source_file_id": normalized_text(item.get("file_id") or item.get("id") or raw.get("path") or raw.get("file_id")) or None,
                    "attachment_type": normalized_text(item.get("type") or item.get("attachment_type")) or None,
                    "raw": item,
                })
            elif normalized_text(item):
                add_record({"file_name": normalized_text(item), "raw": item})
    if not by_name:
        for name in [item.strip() for item in str(row.get("attachment_files") or "").split(";") if item.strip()]:
            add_record({"file_name": name, "raw": name})
    return list(by_name.values())


def attachment_fingerprint(record: dict[str, Any]) -> str:
    stable = "|".join([
        normalized_text(record.get("source_file_id")),
        normalized_text(record.get("file_name")),
        normalized_text(record.get("file_url")),
    ])
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def sync_order_attachments(session: Session, order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    for record in extract_attachment_records(row):
        fingerprint = attachment_fingerprint(record)
        existing = (
            session.query(OrderAttachment)
            .filter(
                OrderAttachment.source_system == order.source_system,
                OrderAttachment.crm_order_id == order.crm_order_id,
                OrderAttachment.payload_hash == digest,
                OrderAttachment.fingerprint == fingerprint,
            )
            .first()
        )
        payload = {
            "crm_sales_order_id": order.id,
            "source_system": order.source_system,
            "crm_order_id": order.crm_order_id,
            "crm_order_no": order.crm_order_no,
            "payload_hash": digest,
            "attachment_type": record.get("attachment_type"),
            "file_name": record["file_name"],
            "file_url": record.get("file_url"),
            "source_file_id": record.get("source_file_id"),
            "fingerprint": fingerprint,
            "parse_status": "Registered",
            "evidence_json": dumps({"source": "crm_order_detail", "payload_hash": digest}),
            "raw_json": dumps(record.get("raw")),
            "captured_at": now_utc(),
        }
        if existing is None:
            existing = OrderAttachment(**payload)
            session.add(existing)
            session.flush()
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        reuse_previous_attachment_evidence(session, existing)
        if existing.file_url:
            cache_order_attachment_file(session, existing)


def reuse_previous_attachment_evidence(session: Session, attachment: OrderAttachment) -> None:
    evidence = loads(attachment.evidence_json, {})
    if evidence.get("parsed_text") or evidence.get("local_storage_ref"):
        return
    query = (
        session.query(OrderAttachment)
        .filter(
            OrderAttachment.source_system == attachment.source_system,
            OrderAttachment.crm_order_id == attachment.crm_order_id,
            OrderAttachment.id != attachment.id,
        )
    )
    if attachment.source_file_id:
        query = query.filter(OrderAttachment.source_file_id == attachment.source_file_id)
    else:
        query = query.filter(OrderAttachment.file_name == attachment.file_name)
    previous = None
    for candidate in query.order_by(OrderAttachment.created_at.desc()).limit(20):
        candidate_evidence = loads(candidate.evidence_json, {})
        if candidate_evidence.get("parsed_text") or candidate_evidence.get("local_storage_ref"):
            previous = candidate
            evidence = candidate_evidence
            break
    if previous is None:
        return
    reused = dict(evidence)
    reused["source"] = "crm_order_detail"
    reused["payload_hash"] = attachment.payload_hash
    reused["reused_from_attachment_id"] = previous.id
    reused["reused_from_payload_hash"] = previous.payload_hash
    attachment.evidence_json = dumps(reused)
    if previous.file_url and (str(previous.file_url).startswith("data/attachments/") or not attachment.file_url):
        attachment.file_url = previous.file_url
    attachment.parse_status = previous.parse_status if previous.parse_status in {"Parsed", "Cached"} else attachment.parse_status


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def infer_currency(settlement_method: str | None) -> str | None:
    text = str(settlement_method or "").strip().upper()
    enum_mapping = {
        "OPTION1": "CNY",
        "RMB": "CNY",
        "人民币": "CNY",
        "元": "CNY",
    }
    if text in enum_mapping:
        return enum_mapping[text]
    if "人民币" in text or "RMB" in text:
        return "CNY"
    for code in ("CNY", "USD", "EUR", "JPY", "HKD"):
        if code in text:
            return code
    return None


def settlement_method_from_items(row: dict[str, Any]) -> str:
    items = row.get("order_items") or row.get("items")
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        value = normalized_text(item.get("settlement_method") or item.get("订单结算方式") or item.get("结算方式"))
        if value:
            return value
    return ""


def crm_order_item_text(item: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = normalized_text(item.get(key))
        if value:
            return value
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    for key in keys:
        value = normalized_text(raw.get(key))
        if value:
            return value
    return None


def sync_crm_order_items(session: Session, order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    session.query(CrmOrderItem).filter(CrmOrderItem.order_id == order.id).delete(synchronize_session=False)
    raw_items = row.get("order_items") or row.get("items")
    if not isinstance(raw_items, list):
        return
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        product_name = crm_order_item_text(item, ["product_name", "name", "productName", "产品名称", "商品名称", "货物名称"])
        specification = crm_order_item_text(item, ["specification", "model", "规格型号", "规格", "型号", "主要规格/详细配置"])
        quantity = crm_order_item_text(item, ["quantity", "qty", "数量"])
        unit_price = crm_order_item_text(item, ["unit_price", "price", "销售单价", "单价", "价格(元)", "不含税单价（元）"])
        line_amount = crm_order_item_text(item, ["line_amount", "amount", "销售订单金额", "小计", "总价", "总金额（含税）", "不含税总价（元）"])
        sku_code = crm_order_item_text(item, ["sku_code", "skuCode", "sku_id", "product_code", "商品编码", "产品编码", "SKU"])
        crm_item_id = crm_order_item_text(item, ["crm_item_id", "item_id", "id", "订单产品编号"]) or f"{order.crm_order_id}:{index + 1}"
        if not any([product_name, specification, quantity, unit_price, line_amount, sku_code]):
            continue
        session.add(
            CrmOrderItem(
                order_id=order.id,
                source_system=order.source_system,
                crm_item_id=crm_item_id,
                crm_order_id=order.crm_order_id,
                crm_order_no=order.crm_order_no,
                sku_code=sku_code,
                product_name=product_name,
                specification=specification,
                quantity=quantity,
                unit_price=unit_price,
                line_amount=line_amount,
                raw_json=dumps(item),
                payload_hash=digest,
                synced_at=now_utc(),
            )
        )


def apply_order_row(order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    def keep_existing(key: str, current: Any) -> str | None:
        value = str(row.get(key) or "").strip()
        if value:
            return value
        existing = str(current or "").strip()
        return existing or None

    def keep_existing_any(keys: list[str], current: Any) -> str | None:
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value:
                return value
        existing = str(current or "").strip()
        return existing or None

    order.crm_order_id = keep_existing("crm_order_id", order.crm_order_id) or ""
    order.crm_order_no = keep_existing("crm_order_no", order.crm_order_no) or ""
    order.customer_id = keep_existing("customer_id", order.customer_id)
    order.customer_name = keep_existing("customer_name", order.customer_name)
    order.opportunity_id = keep_existing("opportunity_id", order.opportunity_id)
    order.opportunity_name = keep_existing("opportunity_name", order.opportunity_name)
    order.sales_user_id = keep_existing("sales_user_id", order.sales_user_id)
    order.sales_user_name = keep_existing_any(["sales_user_name", "owner_name", "ownerName", "owner__r", "owner_display_name"], order.sales_user_name)
    # 销售邮箱优先从 CRM 负责人链接页/人员对象提取；取不到时由通知路由走系统兜底人。
    order.sales_user_email = keep_existing_any(
        [
            "sales_user_email",
            "owner_email",
            "ownerEmail",
            "owner_mail",
            "ownerMail",
            "created_by_email",
            "creator_email",
            "last_modified_by_email",
            "modifier_email",
            "email",
            "salesEmail",
        ],
        order.sales_user_email,
    )
    order.owner_department = keep_existing_any(["owner_department", "owner_main_department", "ownerMainDepartment", "main_department", "department"], order.owner_department)
    order.life_status = keep_existing("life_status", order.life_status)
    order.approval_status = keep_existing("approval_status", order.approval_status)
    order.order_date = keep_existing("order_date", order.order_date)
    if not normalized_text(row.get("settlement_method")):
        item_settlement_method = settlement_method_from_items(row)
        if item_settlement_method:
            row["settlement_method"] = item_settlement_method
    order.settlement_method = keep_existing("settlement_method", order.settlement_method)
    if not normalized_text(order.settlement_method) and not is_overseas_order_payload(row):
        order.settlement_method = DOMESTIC_SETTLEMENT_METHOD
    order.currency = infer_currency(order.settlement_method)
    order.order_amount = keep_existing("order_amount", order.order_amount)
    order.received_amount = keep_existing("received_amount", order.received_amount)
    order.receivable_amount = keep_existing("receivable_amount", order.receivable_amount)
    order.invoice_amount = keep_existing("invoice_amount", order.invoice_amount)
    order.product_amount = keep_existing("product_amount", order.product_amount)
    order.logistics_status = keep_existing("logistics_status", order.logistics_status)
    order.shipment_status = keep_existing("shipment_status", order.shipment_status)
    order.invoice_status = keep_existing("invoice_status", order.invoice_status)
    for attachment_field in ("receipt_contact", "receipt_phone", "receipt_address", "delivery_date"):
        existing_attachment_value = str(getattr(order, attachment_field, None) or "").strip()
        if existing_attachment_value:
            row[attachment_field] = existing_attachment_value
        else:
            row.pop(attachment_field, None)
    order.remark = keep_existing("remark", order.remark)
    attachment_names = [item.strip() for item in str(row.get("attachment_files") or "").split(";") if item.strip()]
    if not attachment_names:
        attachment_names = [normalized_text(item.get("file_name")) for item in row.get("attachments", []) if isinstance(item, dict) and normalized_text(item.get("file_name"))]
    if attachment_names:
        order.attachment_files_json = dumps(attachment_names)
    existing_raw = loads(order.raw_json, {})
    if isinstance(existing_raw, dict) and isinstance(existing_raw.get("oms_field_extraction"), dict) and "oms_field_extraction" not in row:
        row["oms_field_extraction"] = existing_raw["oms_field_extraction"]
    order.raw_json = dumps(row)
    order.payload_hash = digest
    order.sync_status = "Synced"
    order.synced_at = now_utc()
    order.source_created_at = parse_datetime(row.get("created_at"))
    order.source_updated_at = parse_datetime(row.get("updated_at"))
    order.updated_at = now_utc()


def latest_crm_sync_run(session: Session) -> CrmSyncRun | None:
    return session.query(CrmSyncRun).order_by(CrmSyncRun.started_at.desc()).first()


def crm_order_summary(session: Session) -> dict[str, Any]:
    # 改用聚合查询替代全表扫描
    total = session.query(func.count(CrmSalesOrder.id)).scalar() or 0

    def amount_sum(field: str) -> float:
        """仅加载指定金额列，Python 求和（兼容 VARCHAR 逗号金额字段）"""
        total_amount = 0.0
        for (value,) in session.query(getattr(CrmSalesOrder, field)).all():
            if value in (None, ""):
                continue
            try:
                total_amount += float(str(value).replace(",", ""))
            except ValueError:
                continue
        return round(total_amount, 2)

    latest = latest_crm_sync_run(session)
    pending_job = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .order_by(ProcessingJob.created_at)
        .first()
    )

    latest_serialized = serialize_sync_run(latest) if latest else None
    return {
        "total": total,
        "total_orders": total,
        "total_order_amount": amount_sum("order_amount"),
        "total_received_amount": amount_sum("received_amount"),
        "total_receivable_amount": amount_sum("receivable_amount"),
        "last_sync_at": config_value(session, "crm_sales_orders_last_sync_at", ""),
        "sync_enabled": config_bool(session, "crm_sync_enabled", False),
        "sync_interval_seconds": config_int(session, "crm_sync_interval_seconds", 3600),
        "cdp_url": config_value(session, "crm_cdp_url", DEFAULT_CDP_URL),
        "request_file": config_value(session, "crm_fxiaoke_request_file", ""),
        "has_request_json": bool(config_value(session, "crm_fxiaoke_request_json", "").strip()),
        "latest_run": latest_serialized,
        "last_run": latest_serialized,
        "pending_job": {"id": pending_job.id, "status": pending_job.status} if pending_job else None,
    }


def serialize_sync_run(row: CrmSyncRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_system": row.source_system,
        "sync_type": row.sync_type,
        "status": row.status,
        "trigger": row.trigger,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_count": row.created_count,
        "updated_count": row.updated_count,
        "unchanged_count": row.unchanged_count,
        "total_count": row.total_count,
        "error_message": row.error_message,
        "detail": loads(row.detail_json, {}),
    }
