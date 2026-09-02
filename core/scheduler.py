"""定时任务调度 - 账号有效性检测、trial 到期提醒"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlmodel import Session, select
from .db import engine, AccountModel
from .registry import get, load_all
from .base_platform import Account, AccountStatus, RegisterConfig
from services.task_history_retention import cleanup_task_history
import threading
import time


def tick_chatgpt_auto_relogin(*, now: datetime):
    """Lazy import keeps scheduler module startup independent from API tasks."""

    from services.chatgpt_auto_relogin import tick_chatgpt_auto_relogin as tick

    return tick(now=now)


def recover_stale_sms_pool(*, now: datetime) -> int:
    """Lazy import keeps SMS pool startup recovery owned by database init."""

    from core.sms_pool import sms_pool_service

    return sms_pool_service.recover_stale_active(now=now)


def wake_codex2api_target_health() -> bool:
    from services.control_plane_runtime import wake_target_health

    return wake_target_health()


def wake_account_quota_collection() -> bool:
    from services.control_plane_runtime import wake_target_quota

    return wake_target_quota()


def wake_account_pool_planning() -> bool:
    from services.control_plane_runtime import wake_pool_planning

    return wake_pool_planning()


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._loop_interval_seconds = 60
        self._trial_check_interval_seconds = 3600
        self._task_history_cleanup_interval_seconds = 600
        self._sms_pool_recovery_interval_seconds = 600
        self._target_health_interval_seconds = 300
        self._quota_collection_interval_seconds = 300
        self._pool_planning_interval_seconds = 900
        self._last_trial_check_at = 0.0
        self._last_cpa_maintenance_at = 0.0
        self._last_task_history_cleanup_at = 0.0
        self._last_sms_pool_recovery_at = 0.0
        self._last_target_health_at = 0.0
        self._last_quota_collection_at = 0.0
        self._last_pool_planning_at = 0.0

    def start(self):
        with self._lifecycle_lock:
            if self._running:
                return
            self._running = True
            stop_event = threading.Event()
            self._stop_event = stop_event

            now = time.monotonic()
            # 保持 trial/CPA 启动后先等完整周期；自动重登由首轮 tick 自行排期。
            self._last_trial_check_at = now
            self._last_cpa_maintenance_at = now
            self._last_task_history_cleanup_at = (
                now - self._task_history_cleanup_interval_seconds
            )
            self._last_sms_pool_recovery_at = now
            self._last_target_health_at = now - self._target_health_interval_seconds
            self._last_quota_collection_at = now - self._quota_collection_interval_seconds
            self._last_pool_planning_at = now - self._pool_planning_interval_seconds

            self._thread = threading.Thread(
                target=self._loop,
                args=(stop_event,),
                daemon=True,
            )
            self._thread.start()
        print("[Scheduler] 已启动")

    def stop(self):
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()

    def run_once(
        self,
        wall_now: datetime,
        monotonic_now: float,
    ) -> None:
        if wall_now.tzinfo is None:
            wall_now = wall_now.replace(tzinfo=timezone.utc)
        else:
            wall_now = wall_now.astimezone(timezone.utc)

        try:
            tick_chatgpt_auto_relogin(now=wall_now)
        except Exception as e:
            print(f"[Scheduler] ChatGPT 自动重登错误: {e}")

        for last_attr, interval_attr, wake, label in (
            (
                "_last_target_health_at",
                "_target_health_interval_seconds",
                wake_codex2api_target_health,
                "目标健康检查",
            ),
            (
                "_last_quota_collection_at",
                "_quota_collection_interval_seconds",
                wake_account_quota_collection,
                "额度采集",
            ),
            (
                "_last_pool_planning_at",
                "_pool_planning_interval_seconds",
                wake_account_pool_planning,
                "号池计划",
            ),
        ):
            if monotonic_now - getattr(self, last_attr) < getattr(self, interval_attr):
                continue
            try:
                submitted = wake()
            except Exception as e:
                print(f"[Scheduler] Codex2API {label}触发错误: {type(e).__name__}")
            else:
                if submitted:
                    setattr(self, last_attr, monotonic_now)

        if (
            monotonic_now - self._last_sms_pool_recovery_at
            >= self._sms_pool_recovery_interval_seconds
        ):
            try:
                recovered = recover_stale_sms_pool(now=wall_now)
            except Exception as e:
                print(f"[Scheduler] SMS 接码池回收错误: {type(e).__name__}")
            else:
                self._last_sms_pool_recovery_at = monotonic_now
                if recovered:
                    print(f"[Scheduler] 已回收过期 SMS 接码卡密: {recovered} 张")

        if (
            monotonic_now - self._last_task_history_cleanup_at
            >= self._task_history_cleanup_interval_seconds
        ):
            try:
                deleted = cleanup_task_history()
            except Exception as e:
                print(f"[Scheduler] 任务历史清理错误: {type(e).__name__}")
            else:
                self._last_task_history_cleanup_at = monotonic_now
                total_deleted = sum(int(value or 0) for value in deleted.values())
                if total_deleted:
                    print(
                        "[Scheduler] 已清理过期任务历史: "
                        f"任务 {deleted.get('task_runs', 0)} 条，"
                        f"记录 {deleted.get('task_logs', 0)} 条"
                    )

        if (
            monotonic_now - self._last_trial_check_at
            >= self._trial_check_interval_seconds
        ):
            try:
                self.check_trial_expiry()
            except Exception as e:
                print(f"[Scheduler] Trial 检查错误: {e}")
            else:
                self._last_trial_check_at = monotonic_now

        try:
            cpa_interval = self._get_cpa_maintenance_interval_seconds()
            if (
                cpa_interval
                and monotonic_now - self._last_cpa_maintenance_at >= cpa_interval
            ):
                self.check_cpa_credentials()
                self._last_cpa_maintenance_at = monotonic_now
        except Exception as e:
            print(f"[Scheduler] CPA 维护错误: {e}")

    def _loop(self, stop_event: threading.Event | None = None):
        stop_event = stop_event or self._stop_event
        while not stop_event.is_set():
            self.run_once(
                wall_now=datetime.now(timezone.utc),
                monotonic_now=time.monotonic(),
            )
            if stop_event.wait(self._loop_interval_seconds):
                break

    def _get_cpa_maintenance_interval_seconds(self) -> int:
        from services.cpa_manager import get_cpa_maintenance_interval_seconds

        return get_cpa_maintenance_interval_seconds()

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态"""
        now = int(datetime.now(timezone.utc).timestamp())
        with Session(engine) as s:
            accounts = s.exec(
                select(AccountModel).where(AccountModel.status == "trial")
            ).all()
            updated = 0
            for acc in accounts:
                if acc.trial_end_time and acc.trial_end_time < now:
                    acc.status = AccountStatus.EXPIRED.value
                    acc.updated_at = datetime.now(timezone.utc)
                    s.add(acc)
                    updated += 1
            s.commit()
            if updated:
                print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性"""
        load_all()
        with Session(engine) as s:
            q = select(AccountModel).where(
                AccountModel.status.in_(["registered", "trial", "subscribed"])
            )
            if platform:
                q = q.where(AccountModel.platform == platform)
            accounts = s.exec(q.limit(limit)).all()

        results = {"valid": 0, "invalid": 0, "error": 0}
        for acc in accounts:
            try:
                PlatformCls = get(acc.platform)
                plugin = PlatformCls(config=RegisterConfig())
                import json
                account_obj = Account(
                    platform=acc.platform,
                    email=acc.email,
                    password=acc.password,
                    user_id=acc.user_id,
                    region=acc.region,
                    token=acc.token,
                    extra=json.loads(acc.extra_json or "{}"),
                )
                valid = plugin.check_valid(account_obj)
                with Session(engine) as s:
                    a = s.get(AccountModel, acc.id)
                    if a:
                        if acc.platform != "chatgpt":
                            a.status = acc.status if valid else AccountStatus.INVALID.value
                        a.updated_at = datetime.now(timezone.utc)
                        s.add(a)
                        s.commit()
                if valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1
            except Exception:
                results["error"] += 1
        return results

    def check_cpa_credentials(self):
        """清理 CPA 中的 error 凭证，并在低于阈值时自动补注册。"""
        from services.cpa_manager import maintain_cpa_credentials

        return maintain_cpa_credentials()


scheduler = Scheduler()
