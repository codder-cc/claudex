"""Exception hierarchy for claudex."""


class ClaudexError(Exception):
    """Base exception for all claudex errors."""


class ProfileNotFoundError(ClaudexError):
    def __init__(self, name: str):
        super().__init__(f"Profile '{name}' not found. Run 'claudex list' to see available profiles.")
        self.name = name


class ProfileExistsError(ClaudexError):
    def __init__(self, name: str):
        super().__init__(f"Profile '{name}' already exists.")
        self.name = name


class AuthError(ClaudexError):
    """Authentication-related errors."""


class CredentialBackendError(ClaudexError):
    """Credential storage backend errors."""


class ShellIntegrationError(ClaudexError):
    """Shell integration setup errors."""


class SessionNotFoundError(ClaudexError):
    def __init__(self, session_id: str = ""):
        msg = f"Session '{session_id}' not found." if session_id else "No sessions found for this profile."
        super().__init__(msg)
        self.session_id = session_id


class ClaudeNotFoundError(ClaudexError):
    def __init__(self):
        super().__init__(
            "Claude CLI not found in PATH. Install it from https://claude.ai/code"
        )


class ParseError(ClaudexError):
    """JSONL parsing errors."""


class FleetError(ClaudexError):
    """Fleet orchestration errors."""


class JobNotFoundError(FleetError):
    def __init__(self, job_id: str):
        super().__init__(f"Fleet job '{job_id}' not found. Run 'claudex fleet list' to see jobs.")
        self.job_id = job_id


class NoEligibleProfileError(FleetError):
    def __init__(self, detail: str = ""):
        msg = "No eligible profile available to run this job."
        if detail:
            msg += f" {detail}"
        super().__init__(msg)
