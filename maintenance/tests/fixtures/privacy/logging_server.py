"""Fixture: the same handler shape as clean_server.py, but log_message writes
to stderr, so the override no longer suppresses request logging."""
import sys
from http.server import BaseHTTPRequestHandler


class LoggingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        sys.stderr.write("%s - - %s\n" % (self.address_string(), format % args))
