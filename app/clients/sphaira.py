"""
Sphaira client implementation.
"""
from flask import Request, Response, send_from_directory

from .client import BaseClient
from db import Files
from constants import APP_TYPE_FILTERS

SPHAIRA_DEFAULT_HEADERS = [
    'Host',
    'Accept',
    'Accept-Encoding',
]

SPHAIRA_ADDITIONAL_HEADERS = [
    'Authorization',
    'Range',
]

SPHAIRA_HTML_TEMPLATE = '''<!DOCTYPE html>
<html>
<body>
<table>
{}
</table>
</body>
</html>'''

class SphairaClient(BaseClient):
    """Sphaira client with header-based identification, and directory listing support."""

    # Class variables
    CLIENT_NAME = "Sphaira"

    # ==================== Abstract Method Implementations (Required) ====================

    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify Sphaira client by validating required and allowed headers."""
        headers = set(request.headers.keys())

        default_headers = set(SPHAIRA_DEFAULT_HEADERS)
        additional_headers = set(SPHAIRA_ADDITIONAL_HEADERS)

        # All default headers must be present
        if not default_headers.issubset(headers):
            return False

        # Any extra headers must be allowed
        extra_headers = headers - default_headers
        if not extra_headers.issubset(additional_headers):
            return False

        return True

    def error_response(self, error_message: str) -> Response:
        """Generate error response in dir list format."""
        content = [f"{i:02d} - {s}" for i, s in enumerate(['ERROR'] + error_message.split('\n'))]
        return self._serve_directory_listing(content)

    def info_response(self, info_message: str) -> Response:
        """Generate info response in dir list format."""
        content = [f"{i:02d} - {s}" for i, s in enumerate(['INFO'] + info_message.split('\n'))]
        return self._serve_directory_listing(content)

    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for directory listing or file downloads."""
        content_filter = request.content_filter
        subpath = request.subpath

        if content_filter:
            if subpath:
                # this is a file request from a filtered result
                return self._serve_file(subpath)
            if content_filter not in APP_TYPE_FILTERS:
                # this is a file request from a non filtered result
                return self._serve_file(content_filter)

        # Root path or content filter only, return directory listing
        if not content_filter or content_filter in APP_TYPE_FILTERS:
            files = [
                f.filename
                for f in self.get_filtered_files(None if not content_filter else content_filter)
            ]
            return self._serve_directory_listing(files)


    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_head(self, request: Request) -> Response:
        """
        Handle HEAD requests for file lookups.
        Sphaira sends HEAD requests to filenames to get file headers before downloading.
        """
        return self._serve_file(request.subpath if request.subpath else request.content_filter)

    # ==================== Private/Helper Methods ====================

    def _serve_directory_listing(self, content: list[str] | str) -> Response:
        """Serve a Sphaira compatible directory listing."""
        if not content:
            content = '<a href="No content available."> </a>'
        elif isinstance(content, list):
            content = '\n'.join(f'<a href="{item}"> </a>' for item in content)
        else:
            content = f'<a href="{content}"> </a>'
        html = SPHAIRA_HTML_TEMPLATE.format(content)
        return Response(html)

    def _serve_file(self, filename: str) -> Response: 
        """Serve a file from the given filename."""
        # Look up the file in the database by filename
        file = Files.query.filter_by(filename=filename).first()

        if not file:
            self.log_warning(f"File not found: {filename}")
            # Throws NspBadMagic for HEAD requests anyway
            return self.error_response("File not found")

        self.log_info(f"Serving file: {file.folder}/{filename}")

        return send_from_directory(file.folder, filename)
