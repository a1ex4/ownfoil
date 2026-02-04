from .prowlarr import ProwlarrClient, pick_best_result, filter_results
from .torrent_client import test_torrent_client, add_torrent, list_completed, remove_torrent
from .manager import run_downloads_job, manual_search_update, queue_download_url, search_update_options, check_completed_downloads, get_downloads_state

__all__ = [
    "ProwlarrClient",
    "pick_best_result",
    "filter_results",
    "test_torrent_client",
    "add_torrent",
    "remove_torrent",
    "list_completed",
    "run_downloads_job",
    "manual_search_update",
    "queue_download_url",
    "check_completed_downloads",
    "get_downloads_state",
    "search_update_options",
]
