"""Pure-logic helpers for Discord thread permission correctness.

Discord models thread-related permissions as bits in a 64-bit permission
value.  These helpers answer three questions:

* can the bot create a thread of the requested visibility at all?
* can it fall back to sending messages in the parent channel / existing
  threads instead?
* what is the specific permission gap, if any?
"""

CREATE_PUBLIC_THREADS = 1 << 43
"""Permission to create public threads (Discord permission bit 43)."""

CREATE_PRIVATE_THREADS = 1 << 44
"""Permission to create private threads (Discord permission bit 44)."""

SEND_MESSAGES_IN_THREADS = 1 << 38
"""Permission to send messages in threads (Discord permission bit 38)."""


class ThreadPermissionError(ValueError):
    """Raised when ``permission_bits`` is not a non-negative integer."""


def _validate(permission_bits):
    if isinstance(permission_bits, bool) or not isinstance(permission_bits, int):
        raise ThreadPermissionError(
            "permission_bits must be a non-negative integer, "
            f"got {permission_bits!r}"
        )
    if permission_bits < 0:
        raise ThreadPermissionError(
            f"permission_bits must be non-negative, got {permission_bits!r}"
        )


def can_create_thread(permission_bits, *, private):
    """Return True when a thread of the requested visibility can be created.

    Creating a private thread requires CREATE_PRIVATE_THREADS; creating a
    public thread requires CREATE_PUBLIC_THREADS.  Either way the caller
    must also hold SEND_MESSAGES_IN_THREADS.
    """
    _validate(permission_bits)
    if not (permission_bits & SEND_MESSAGES_IN_THREADS):
        return False
    create_bit = CREATE_PRIVATE_THREADS if private else CREATE_PUBLIC_THREADS
    return bool(permission_bits & create_bit)


def fallback_eligible(permission_bits):
    """Return True when the caller can still send in existing threads.

    Holding SEND_MESSAGES_IN_THREADS means the bot can fall back to
    messaging in the parent channel / existing threads even when it cannot
    create a new thread.
    """
    _validate(permission_bits)
    return bool(permission_bits & SEND_MESSAGES_IN_THREADS)


def classify_thread_permission_failure(permission_bits, *, private):
    """Classify why thread creation is not possible, or return ``'ok'``.

    Returns one of:

    * ``'ok'`` -- thread creation is permitted.
    * ``'missing_create_private'`` -- a private thread was requested but
      CREATE_PRIVATE_THREADS is absent (sending in threads is still
      possible).
    * ``'missing_create_public'`` -- a public thread was requested but
      CREATE_PUBLIC_THREADS is absent (sending in threads is still
      possible).
    * ``'missing_send_threads'`` -- the required create bit is present but
      SEND_MESSAGES_IN_THREADS is absent.
    * ``'no_fallback'`` -- neither the required create bit nor
      SEND_MESSAGES_IN_THREADS is present: creation is impossible and there
      is no fallback channel either.
    """
    _validate(permission_bits)
    if can_create_thread(permission_bits, private=private):
        return "ok"
    create_bit = CREATE_PRIVATE_THREADS if private else CREATE_PUBLIC_THREADS
    has_create = bool(permission_bits & create_bit)
    has_send = bool(permission_bits & SEND_MESSAGES_IN_THREADS)
    if not has_create and not has_send:
        return "no_fallback"
    if not has_create:
        return "missing_create_private" if private else "missing_create_public"
    return "missing_send_threads"
