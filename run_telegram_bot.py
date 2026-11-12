import os
import time
import logging
import requests
from typing import Optional
from src.alerts.telegram import TelegramConfig, TelegramAlertService
from src.alerts.bot_handler import TelegramBotHandler
from src.storage.database import StorageManager
from src.alerts.outbox_dispatcher import OutboxNotificationDispatcher
from src.paper.position_monitor import PositionMonitorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("telegram_bot_runner")


def run_telegram_bot_listener(poll_interval: float = 2.0):
    """
    Continuous long-polling listener for interactive Telegram Copilot.
    Processes commands (/menu, /analyze, /account, /watchlist, /settings, /dashboard)
    and trade approvals/rejections via inline buttons.
    """
    config = TelegramConfig.from_env()
    if not config.enabled or not config.bot_token:
        logger.error(
            "Telegram bot is disabled or TELEGRAM_BOT_TOKEN is not set.\n"
            "Please ensure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set before running."
        )
        return

    telegram_svc = TelegramAlertService(config)
    storage = StorageManager()
    handler = TelegramBotHandler(telegram_service=telegram_svc, storage=storage)
    outbox_dispatcher = OutboxNotificationDispatcher(storage=storage, telegram_service=telegram_svc)
    position_monitor = PositionMonitorService(storage=storage, telegram_service=telegram_svc)

    # Single-instance lock to prevent duplicate polling instances
    # Guard against container restarts, PID namespace differences, and stale locks
    lock_file = os.getenv("TELEGRAM_LOCK_FILE", "trading_bot_telegram.lock")
    my_pid = os.getpid()

    def remove_lock():
        try:
            if os.path.exists(lock_file):
                with open(lock_file, "r") as f:
                    content = f.read().strip()
                if content == str(my_pid):
                    os.remove(lock_file)
        except Exception:
            pass

    import atexit
    atexit.register(remove_lock)

    try:
        if os.path.exists(lock_file):
            try:
                with open(lock_file, "r") as f:
                    old_pid_str = f.read().strip()
                if old_pid_str:
                    old_pid = int(old_pid_str)
                    # If the lock file has our own PID, it's this process
                    if old_pid != my_pid:
                        is_active_competing_bot = False
                        try:
                            import psutil
                            if psutil.pid_exists(old_pid):
                                try:
                                    proc = psutil.Process(old_pid)
                                    cmdline = " ".join(proc.cmdline()).lower()
                                    if "telegram" in cmdline or "run_telegram_bot" in cmdline:
                                        is_active_competing_bot = True
                                        logger.error(f"Another instance of Telegram bot listener is already running (PID {old_pid}: {cmdline}). Exiting.")
                                        return
                                    else:
                                        logger.warning(f"Found stale lock file for non-telegram PID {old_pid} ({proc.name()}). Reclaiming lock.")
                                except (psutil.NoSuchProcess, psutil.AccessDenied):
                                    pass
                        except ImportError:
                            # Standard library fallback when psutil is not available
                            # On POSIX (Linux/Docker container), os.kill(pid, 0) checks if process exists
                            try:
                                os.kill(old_pid, 0)
                                # In Docker container, PID 1 is the main container process.
                                # If old_pid is 1 and my_pid is 1, it's ourselves (handled above).
                                # If old_pid is 1 but my_pid != 1, PID 1 is init/uvicorn, not another telegram bot.
                                if old_pid != 1:
                                    logger.error(f"Another process with PID {old_pid} is active. Exiting.")
                                    return
                                else:
                                    logger.warning("Found stale lock for PID 1 in container. Reclaiming lock.")
                            except (OSError, PermissionError, AttributeError):
                                pass
            except ValueError:
                pass
            except Exception as e:
                logger.warning(f"Error checking existing lock file: {e}")

        with open(lock_file, "w") as f:
            f.write(str(my_pid))
    except Exception as e:
        logger.warning(f"Could not write lock file: {e}")

    logger.info("==================================================")
    logger.info("🤖 Starting Interactive Telegram Copilot Listener")
    logger.info("Simulated paper trading only (LIVE EXECUTION DISABLED)")
    logger.info("==================================================")

    # Register Telegram slash commands (/menu, /analyze, /account, etc.)
    cmd_reg = telegram_svc.set_bot_commands()
    if cmd_reg:
        logger.info("Successfully registered Telegram slash command menu.")
    else:
        logger.warning("Could not register Telegram slash commands with Telegram API.")

    # Test connectivity safely
    test_ok = telegram_svc.send_test_alert()
    if test_ok:
        logger.info("Initial connection test message dispatched successfully.")
    else:
        logger.warning("Could not dispatch initial test message. Checking Telegram API reachability...")

    offset: Optional[int] = None
    url = f"https://api.telegram.org/bot{config.bot_token}/getUpdates"
    last_monitor_run = 0.0
    monitor_interval = 15.0  # Run position monitoring cycle every 15 seconds

    while True:
        try:
            # Periodic continuous paper position monitoring
            now_mono = time.time()
            if now_mono - last_monitor_run >= monitor_interval:
                last_monitor_run = now_mono
                try:
                    position_monitor.monitor_all_open_positions()
                except Exception as mon_err:
                    logger.error(f"Error in position monitor cycle: {mon_err}")

            # Drain and dispatch any pending atomic outbox notifications
            try:
                outbox_dispatcher.dispatch_pending(limit=25)
            except Exception as outbox_err:
                logger.error(f"Error draining outbox notifications: {outbox_err}")

            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                updates = data.get("result", [])
                for update in updates:
                    update_id = update["update_id"]
                    offset = update_id + 1
                    try:
                        handler.process_incoming_update(update)
                    except Exception as err:
                        logger.error(f"Error handling update {update_id}: {err}")
            elif resp.status_code == 404:
                logger.error("Telegram API returned 404. Verify TELEGRAM_BOT_TOKEN.")
                time.sleep(10)
            else:
                logger.warning(f"Telegram getUpdates returned HTTP {resp.status_code}: {resp.text[:100]}")
                time.sleep(5)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Network error polling Telegram updates: {e}")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Telegram Copilot listener stopped by user.")
            break
        except Exception as e:
            logger.exception(f"Unexpected error in polling loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_telegram_bot_listener()
