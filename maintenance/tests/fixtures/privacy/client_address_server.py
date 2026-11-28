"""Fixture: log_message is clean, but a different handler method reads the
peer address directly - the property G8 checks holds only when nothing
anywhere in the file does that."""
from http.server import BaseHTTPRequestHandler


class AuditingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._audit(self.client_address[0])
        self.send_response(200)
        self.end_headers()

    def _audit(self, ip):
        pass

    def log_message(self, format, *args):
        pass
