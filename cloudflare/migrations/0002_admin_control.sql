CREATE TABLE IF NOT EXISTS bot_activity (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    command_name TEXT,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_bot_activity_created_at
    ON bot_activity(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bot_activity_command
    ON bot_activity(event_type, command_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_users_status_activity
    ON users(status, last_active_at);

CREATE INDEX IF NOT EXISTS idx_users_registered_at
    ON users(registered_at DESC);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (admin_user_id) REFERENCES users(user_id),
    FOREIGN KEY (target_user_id) REFERENCES users(user_id)
);
