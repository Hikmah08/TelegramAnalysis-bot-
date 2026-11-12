import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.alerts import TelegramConfig, TelegramAlertService


def main():
    print("=" * 65)
    print(" Telegram Alert Integration Diagnostic & Test Utility ")
    print("=" * 65)

    config = TelegramConfig.from_env()

    # Mask credentials if present
    token_preview = (config.bot_token[:4] + "..." + config.bot_token[-4:]) if config.bot_token and len(config.bot_token) > 8 else "NOT SET"
    chat_preview = str(config.chat_id) if config.chat_id else "NOT SET"

    print(f"Status:       {'ENABLED' if config.enabled else 'DISABLED (Default / Offline mode)'}")
    print(f"Bot Token:    {token_preview}")
    print(f"Chat ID:      {chat_preview}")
    print("-" * 65)

    service = TelegramAlertService(config)

    if not config.bot_token or not config.chat_id:
        print("[INFO] Environment variables not fully detected in this shell session.")
        print("You can either set them via Command Prompt, or enter them now for this test:")
        try:
            input_token = input("Enter Telegram Bot Token (or press Enter to skip): ").strip()
            if input_token:
                input_chat = input("Enter Telegram Chat ID (or press Enter to skip): ").strip()
                config = TelegramConfig(
                    bot_token=input_token.strip().strip("'\""),
                    chat_id=input_chat.strip().strip("'\""),
                    enabled=True,
                )
        except (EOFError, KeyboardInterrupt):
            pass

    if not config.bot_token:
        print("\nTELEGRAM_BOT_TOKEN is not set.")
        print("To set in Windows Command Prompt (CMD):")
        print("   set TELEGRAM_BOT_TOKEN=your_token_here")
        print("   set TELEGRAM_CHAT_ID=your_chat_id_here")
        return

    if "--discover" in sys.argv or not config.chat_id:
        print("[INFO] Querying Telegram for your numeric Chat ID...")
        updates = TelegramAlertService.discover_chat_id(config.bot_token)
        if updates:
            print("\nFound the following active chats with your bot:")
            for u in updates:
                print(f"  * Chat ID: {u['chat_id']} | User: @{u['username']} ({u['first_name']}) | Type: {u['type']}")
            print("\nTo configure your Chat ID, run in PowerShell:")
            print(f"  $env:TELEGRAM_CHAT_ID=\"{updates[-1]['chat_id']}\"")
        else:
            print("No recent messages found for this bot.")
            print("Please open Telegram, find your bot, click Start (or send 'hello'), then run:")
            print("  python test_telegram.py --discover")
        if not config.chat_id:
            return

    # Attempt test message
    print("\nSending verification alert...")
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = {
        "chat_id": config.chat_id,
        "text": "🤖 <b>Trading bot Telegram connection test successful.</b>\nAll systems operational.",
        "parse_mode": "HTML",
    }
    try:
        import requests
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print("SUCCESS! Verification alert delivered to your Telegram account.")
        else:
            print(f"Telegram API response: HTTP {resp.status_code}")
            try:
                data = resp.json()
                print(f"Telegram Error: {data.get('description', resp.text)}")
            except Exception:
                print(f"Telegram Response: {resp.text}")
            print("\nTroubleshooting HTTP 404 / Errors:")
            print("  - If 'Not Found': Telegram's API returns 404 specifically when the Bot Token is invalid, mistyped, or revoked.")
            print("  - Ensure you did not include quotes, leading/trailing spaces, or the word 'bot' twice.")
            print("  - Test your token in browser (masked): https://api.telegram.org/bot<YOUR_TOKEN>/getMe")
    except Exception as e:
        print(f"Network / connection error: {e}")
    print("=" * 65)


if __name__ == "__main__":
    main()
