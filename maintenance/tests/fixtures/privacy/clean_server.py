"""Fixture: a handler whose log_message override does nothing but pass, and
which never reads the peer address anywhere in the file."""
from http.server import BaseHTTPRequestHandler


class CleanHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        """quiet - no request data is ever written anywhere"""
        pass
