"""Main entry point for fcitx5-voice daemon (streaming mode)."""

import argparse
from argparse import BooleanOptionalAction
import atexit
import logging
import signal
import sys

from gi.repository import GLib

from .dbus_service import start_dbus_service
from .ws_client import DEFAULT_URL, DEFAULT_MODEL, DEFAULT_LANGUAGE

# Global service instance for cleanup
service = None


def setup_logging(debug: bool = False):
    """Configure logging for the daemon."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cleanup():
    """Clean up resources on exit."""
    global service
    if service:
        service.cleanup()
    logging.info("fcitx5-voice daemon stopped")


def signal_handler(sig, frame):
    """Handle interrupt signals."""
    logging.info(f"Received signal {sig}, shutting down...")
    sys.exit(0)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="fcitx5 Voice Input Daemon (GPU streaming mode)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"WebSocket server URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=f"Language code (default: {DEFAULT_LANGUAGE})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"ASR model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--compression",
        action=BooleanOptionalAction,
        default=True,
        help="Enable WebSocket compression (permessage-deflate). Use --no-compression to disable.",
    )
    args = parser.parse_args()

    setup_logging(args.debug)
    # Suppress noisy websockets debug logs (audio frame dumps)
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.info("Starting fcitx5-voice daemon (streaming mode)")

    # Register cleanup handlers
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start D-Bus service
    global service
    try:
        service = start_dbus_service(
            ws_url=args.url,
            model=args.model,
            language=args.language,
            compression="deflate" if args.compression else None,
        )
    except Exception as e:
        logging.error(f"Failed to start D-Bus service: {e}")
        sys.exit(1)

    # Run GLib main loop for D-Bus
    logging.debug("Entering main loop")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
