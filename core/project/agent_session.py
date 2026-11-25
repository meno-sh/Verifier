"""The run options of the daemon: when the agent gets a new session, and the claim step timeout.

Each model call of a session sends the whole conversation again, so a long session costs
more for each round. A new session reads none of it. The claim files hold the results of
the work, so a new session loses no result: it loses only what the agent learned about
the project, and the daemon gives it a short briefing (runtime/daemon.py).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from core.registry import run_options

ONE_SESSION = "one-session"                     # one session for the whole run
FRESH_EACH_ROUND = "fresh-each-round"           # a new session before each round
# a new session before a round when the current one has read the token limit
FRESH_AFTER_TOKEN_LIMIT = "fresh-after-token-limit"
POLICY_KINDS = (ONE_SESSION, FRESH_EACH_ROUND, FRESH_AFTER_TOKEN_LIMIT)
DEFAULT_TOKEN_LIMIT = 2_000_000
DEFAULT_CLAIM_STEPS_TIMEOUT = 30 * 60

# The run options of the daemon; core/project/watched_project.py adds them to a watched project.
POLICY = run_options.RunOption(
    key="agent_session_policy", kind=str, flag="--agent-session-policy",
    default=FRESH_EACH_ROUND,
    help=f"when the auditing agent gets a new session: {', '.join(POLICY_KINDS)}")
TOKEN_LIMIT = run_options.RunOption(
    key="agent_session_token_limit", kind=int, flag="--agent-session-token-limit",
    default=DEFAULT_TOKEN_LIMIT,
    help=f"with {FRESH_AFTER_TOKEN_LIMIT}: the tokens a session reads before the agent "
         "gets a new one")
# The claim steps themselves, and their order, are CLAIM_STEPS and LINK_STEPS in
# runtime/daemon.py.
CLAIM_STEPS_TIMEOUT = run_options.RunOption(
    key="claim_steps_timeout", kind=int, flag="--claim-steps-timeout",
    default=DEFAULT_CLAIM_STEPS_TIMEOUT,
    help="seconds the daemon waits for each claim step that code does")
RUN_OPTIONS = (POLICY, TOKEN_LIMIT, CLAIM_STEPS_TIMEOUT)


def claim_steps_timeout(options: Mapping[str, object]) -> int:
    """The timeout of one claim step in the run options, or its default when it is not set.

    Raises:
        run_options.OptionsError: the timeout is not a whole number of at least 1 second.
    """
    try:
        value = CLAIM_STEPS_TIMEOUT.value_in_options(options)
    except (TypeError, ValueError) as e:
        raise run_options.OptionsError(str(e)) from None
    assert isinstance(value, int), value
    if value < 1:
        raise run_options.OptionsError(
            f"claim_steps_timeout must be at least 1 second, got {value}")
    return value


class AgentSessionError(ValueError):
    """A run option of the agent session has a value the policy refuses."""


@dataclasses.dataclass(frozen=True)
class AgentSessionPolicy:
    """Which rounds start in a new session of the auditing agent."""
    kind: str = FRESH_EACH_ROUND
    token_limit: int = DEFAULT_TOKEN_LIMIT

    def __post_init__(self) -> None:
        """Refuse an unknown kind, or a token limit below 1."""
        if self.kind not in POLICY_KINDS:
            raise AgentSessionError(f"the agent session policy must be one of "
                                    f"{', '.join(POLICY_KINDS)}, not {self.kind!r}")
        if self.token_limit < 1:
            raise AgentSessionError(
                f"the agent session token limit must be at least 1, got {self.token_limit}")

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> AgentSessionPolicy:
        """The policy from the run options, each at its default when it is not set.

        Raises:
            AgentSessionError: an option is set to a value the policy refuses.
        """
        kind, limit = POLICY.value_in_options(options), TOKEN_LIMIT.value_in_options(options)
        assert isinstance(kind, str) and isinstance(limit, int), (kind, limit)  # their kinds
        return cls(kind=kind, token_limit=limit)

    def needs_fresh_session(self, rounds_in_session: int, tokens_read: int) -> bool:
        """Whether the next round starts in a new session.

        Args:
            rounds_in_session: the rounds the current session already ran.
            tokens_read: the tokens the model calls of the current session read: their
                input tokens and their cache read tokens.
        """
        if rounds_in_session == 0 or self.kind == ONE_SESSION:
            return False
        if self.kind == FRESH_EACH_ROUND:
            return True
        return tokens_read >= self.token_limit
