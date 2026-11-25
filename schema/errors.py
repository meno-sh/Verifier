"""The root class of every error that the Verifier raises on purpose.

Each error class stays in the module that raises it, and is a subclass of
VerifierError. An error that is also a ValueError now keeps that base too, so code
that catches ValueError works as before.
"""


class VerifierError(Exception):
    """An input or a state that the Verifier refuses, with the reason in the message."""
