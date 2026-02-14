"""Main entry point for fcitx5-voice daemon."""
import argparse
import atexit
import logging
import signal
import sys

from gi.repository import GLib

from .dbus_service import start_dbus_service

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
    parser = argparse.ArgumentParser(description="fcitx5 Voice Input Daemon")
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    setup_logging(args.debug)
    logging.info("Starting fcitx5-voice daemon")

    # Register cleanup handlers
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start D-Bus service
    global service
    try:
        service = start_dbus_service()
    except Exception as e:
        logging.error(f"Failed to start D-Bus service: {e}")
        sys.exit(1)

    # Run GLib main loop for D-Bus
    logging.info("Entering main loop")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
