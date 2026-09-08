"""LAN discovery: answers the UDP broadcast clients send to find shops on the local network.

The wire format belongs to the client: a datagram containing exactly OWNFOIL_DISCOVER, answered
with a JSON object carrying the magic, this shop's identity and the addresses to reach it on.
"""
import json
import logging
import socket
import threading

from constants import APP_VERSION, DISCOVERY_MAGIC, DISCOVERY_PORT, DISCOVERY_REQUEST, HTTP_PORT
from settings import get_server_uid, get_settings
from utils import get_lan_ip

logger = logging.getLogger('main')

_responder = None
_lock = threading.Lock()


def discovery_payload():
    """The reply describing this shop: who it is, and where a console can reach it."""
    shop = get_settings()['shop']
    lan_ip = get_lan_ip()
    return {
        'magic': DISCOVERY_MAGIC,
        'uid': get_server_uid(),
        'name': shop['name'],
        'version': APP_VERSION,
        # Reachable here on this network, and there from anywhere else - either may be empty.
        'local': f'{lan_ip}:{HTTP_PORT}' if lan_ip else '',
        'remote': shop['host'],
        'public': shop['public'],
    }


class DiscoveryResponder(threading.Thread):
    """Daemon thread answering discovery requests until stopped."""

    def __init__(self, port=DISCOVERY_PORT):
        super().__init__(daemon=True)
        self.port = port
        self.sock = None
        self._stopping = False

    def bind(self):
        """Take the UDP port, returning whether the service can run at all."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('', self.port))
        except OSError as e:
            sock.close()
            logger.warning(f'LAN discovery disabled: cannot listen on UDP port {self.port} ({e}).')
            return False
        self.sock = sock
        self.port = sock.getsockname()[1]
        return True

    def run(self):
        while not self._stopping:
            try:
                request, sender = self.sock.recvfrom(1024)
            except OSError:
                break  # stop() closed the socket under us
            if request.strip() != DISCOVERY_REQUEST:
                continue
            try:
                self.sock.sendto(json.dumps(discovery_payload()).encode(), sender)
            except OSError as e:
                logger.warning(f'Could not answer discovery request from {sender[0]}: {e}')

    def stop(self):
        """Close the socket so the blocked recvfrom returns and the thread ends."""
        self._stopping = True
        self.sock.close()
        self.join(timeout=5)


def reconcile():
    """Start or stop the responder to match the configured services."""
    global _responder
    with _lock:
        if get_settings()['services']['discovery']['enabled']:
            if _responder is None:
                responder = DiscoveryResponder(DISCOVERY_PORT)
                if responder.bind():
                    responder.start()
                    _responder = responder
                    logger.info(f'LAN discovery listening on UDP port {responder.port}.')
        elif _responder is not None:
            _responder.stop()
            _responder = None
            logger.info('LAN discovery stopped.')


def stop():
    """Stop answering, whatever the settings say."""
    global _responder
    with _lock:
        if _responder is not None:
            _responder.stop()
            _responder = None
