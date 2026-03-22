"""Textual CSS theme for claudex."""

APP_CSS = """
/* ─── Global ─────────────────────────────────────────── */
Screen {
    background: $background;
}

/* ─── Tab Bar ─────────────────────────────────────────── */
TabbedContent {
    height: 1fr;
}

TabPane {
    padding: 0 1;
    height: 1fr;
}

/* Widgets fill their tab pane */
DashboardWidget, HistoryWidget, AuthManagerWidget, SettingsWidget {
    height: 1fr;
}

/* ─── Profile Table ───────────────────────────────────── */
#profile-table {
    height: 1fr;
    border: tall $primary;
}

DataTable {
    height: 1fr;
}

DataTable > .datatable--header {
    background: $primary-darken-1;
    color: $text;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: $accent;
    color: $text;
}

/* ─── Detail Panel ────────────────────────────────────── */
#detail-panel {
    width: 40;
    border: tall $primary-darken-1;
    padding: 1 2;
    background: $surface;
}

#detail-title {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}

/* ─── Auth Status Badges ──────────────────────────────── */
.auth-ok {
    color: $success;
}

.auth-expired {
    color: $error;
}

.auth-none {
    color: $text-muted;
}

.auth-api {
    color: $warning;
}

/* ─── History Screen ──────────────────────────────────── */
#search-bar {
    dock: top;
    height: 3;
    padding: 0 1;
}

#search-input {
    width: 1fr;
    border: tall $primary;
}

#profile-filter {
    width: 20;
    margin-left: 1;
}

#session-table {
    height: 1fr;
    border: tall $primary;
}

#session-detail {
    height: 10;
    border: tall $primary-darken-1;
    padding: 1 2;
    background: $surface;
}

/* ─── Auth Manager ────────────────────────────────────── */
#auth-list {
    width: 30;
    border: tall $primary;
    height: 1fr;
}

#auth-detail {
    width: 1fr;
    border: tall $primary-darken-1;
    padding: 1 2;
    height: 1fr;
    background: $surface;
}

#auth-actions {
    height: auto;
    margin-top: 1;
}

Button {
    margin: 0 1 1 0;
}

Button.action-btn {
    min-width: 20;
}

Button.danger-btn {
    background: $error-darken-1;
    color: $text;
}

/* ─── Settings Screen ─────────────────────────────────── */
#settings-content {
    padding: 1 2;
    height: 1fr;
    overflow-y: auto;
}

.settings-section-title {
    text-style: bold;
    color: $accent;
    margin-top: 1;
    margin-bottom: 0;
}

.settings-row {
    height: 3;
    margin-bottom: 0;
}

/* ─── Modal / Dialog ──────────────────────────────────── */
#modal-overlay {
    align: center middle;
}

#dialog-box {
    width: 60;
    height: auto;
    border: double $accent;
    padding: 1 2;
    background: $surface;
}

#dialog-title {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}

#dialog-buttons {
    align: right middle;
    height: 3;
}

/* ─── Status Bar ──────────────────────────────────────── */
#status-bar {
    dock: bottom;
    height: 1;
    background: $primary-darken-3;
    color: $text-muted;
    padding: 0 1;
}

/* ─── Notification / Toast ────────────────────────────── */
Toast {
    background: $surface;
    border: tall $accent;
}

/* ─── Input fields ────────────────────────────────────── */
Input {
    border: tall $primary;
}

Input:focus {
    border: tall $accent;
}

/* ─── New Profile Form ────────────────────────────────── */
#new-profile-form {
    width: 60;
    height: auto;
    border: double $primary;
    padding: 1 2;
    background: $surface;
    align: center middle;
}

.form-label {
    margin-top: 1;
    color: $text-muted;
}

.form-input {
    width: 1fr;
    margin-bottom: 0;
}
"""
