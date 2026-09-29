"""
Sphaira client implementation.
"""
import os

from flask import Request, Response

from .client import BaseClient
from db import db, Files, Libraries, send_library_file
from constants import APP_TYPE_FILTERS, ALLOWED_EXTENSIONS

# Sphaira announces itself as `Sphaira/<version>` since version 1.0.6.
SPHAIRA_USER_AGENT = 'Sphaira/'

SPHAIRA_HTML_TEMPLATE = '''<!DOCTYPE html>
<html>
<body>
<table>
{}
</table>
</body>
</html>'''

class SphairaClient(BaseClient):
    """Sphaira client with User-Agent identification, and directory listing support."""

    # Class variables
    CLIENT_NAME = "Sphaira"

    # ==================== Abstract Method Implementations (Required) ====================

    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify Sphaira client by its User-Agent."""
        return request.headers.get('User-Agent', '').lower().startswith(SPHAIRA_USER_AGENT.lower())

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
        subpath = request.path.strip('/')
        paths = subpath.split('/')
        # Check if requesting a specific file
        if paths and any([paths[-1].endswith(ext) for ext in ALLOWED_EXTENSIONS]):
            return self._serve_file(subpath)

        # Otherwise, show directory listing
        content_filter = paths[0] if paths and paths[0] in APP_TYPE_FILTERS else None
        return self._serve_virtual_directory(subpath, content_filter)


    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_head(self, request: Request) -> Response:
        """
        Handle HEAD requests for file lookups.
        Sphaira sends HEAD requests to filenames to get file headers before downloading.
        """
        subpath = request.path.strip('/')
        if any([subpath.endswith(ext) for ext in ALLOWED_EXTENSIONS]):
            return self._serve_file(subpath)
        return self.error_response("File not found")

    # ==================== Private/Helper Methods ====================

    def _serve_virtual_directory(self, path: str, content_filter: str = None) -> Response:
        """
        Serve a virtual directory listing by recreating folder structure.
        Strips library path from file paths and shows directories/files at current level.
        """
        # Get all filtered files
        all_files = self.get_filtered_files(content_filter)
        if content_filter:
            path = path[len(content_filter):].lstrip('/')
        # Build virtual paths by stripping library paths
        virtual_items = set()
        
        for file in all_files:
            # Get the library path for this file
            library = Libraries.query.filter_by(id=file.library_id).first()
            
            library_path = library.path.rstrip('/' + os.sep)
            file_path = file.filepath

            # Strip library path to get relative path, in the url form the listing is built in:
            # filepaths come off the filesystem in its own separator.
            relative_path = file_path[len(library_path):].lstrip('/' + os.sep).replace(os.sep, '/')
            
            # If we're in a subdirectory, filter to only show items under current path
            if path:
                if not relative_path.startswith(path + '/'):
                    continue
                # Get the remainder after the current path
                remainder = relative_path[len(path) + 1:]
            else:
                remainder = relative_path
            
            # Get the next level item (directory or file)
            if '/' in remainder:
                # This is a directory - get just the directory name
                next_item = remainder.split('/')[0] + '/'
            else:
                # This is a file at the current level
                next_item = remainder
            
            virtual_items.add(next_item)
        
        # Sort items: directories first, then files
        sorted_items = sorted(virtual_items, key=lambda x: (not x.endswith('/'), x.lower()))
        
        if not sorted_items:
            return self._serve_directory_listing(['No content available'])
        
        return self._serve_directory_listing(sorted_items)

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

    def _serve_file(self, path: str) -> Response:
        """Serve the file at a virtual path, the one the directory listing links to."""
        paths = path.split('/')
        if paths[0] in APP_TYPE_FILTERS:
            paths = paths[1:]
        # The listing merges every library into one tree, so the path may lie under any of them.
        candidates = [os.path.join(library.path, *paths) for library in Libraries.query.all()]
        filepath = db.session.query(Files.filepath).filter(
            Files.filepath.in_(candidates)).order_by(Files.id).limit(1).scalar()

        if not filepath:
            self.log_warning(f"File not found: {path}")
            # Throws NspBadMagic for HEAD requests anyway
            return self.error_response("File not found")

        self.log_info(f"Serving file: {filepath}")
        return send_library_file(filepath)
