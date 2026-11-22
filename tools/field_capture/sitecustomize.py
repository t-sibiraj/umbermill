"""Install the observer in an explicitly configured original test run and its child interpreters."""
import os

if "FIELD_TRACE_DIR" in os.environ and "FIELD_CAPTURE_SOURCE" in os.environ:
    from field_capture_observer import install
    install()

if "DISPATCH_RECEIPTS" in os.environ and "DISPATCH_OWNER" in os.environ:
    from effective_dispatch import child_install
    child_install()
