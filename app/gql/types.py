"""Strawberry GraphQL types for ownfoil.

Batch-loaded fields use `null` to mean "not loaded on this path, or not exposed to
this role" and `[]` to mean "loaded, and there is nothing". The two are not the same
thing, and no client can tell them apart from the value alone - so every such field
says in its description which paths hydrate it.
"""
import json
import strawberry
from strawberry import Private
from typing import List, Optional

from .filters import AppFilter, FileFilter, match_app, match_file
from .scalars import BigInt


def decode_json_list(value) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if isinstance(decoded, list):
        return [str(x) for x in decoded]
    return None


@strawberry.type
class Ownership:
    have_base: bool
    up_to_date: bool
    complete: bool


@strawberry.type
class File:
    id: strawberry.ID
    library_id: int
    filename: str
    folder: Optional[str] = None
    extension: Optional[str] = None
    size: Optional[BigInt] = None
    compressed: bool = False
    multicontent: bool = False
    nb_content: int = 0
    download_count: int = 0
    identified: bool = False
    identification_type: Optional[str] = None
    identification_error: Optional[str] = None
    identification_attempts: int = 0
    organized: bool = False
    mtime: Optional[float] = None
    filepath: Optional[str] = None  # admin-only; null for non-admin

    # Apps linked to this file via the app_files m2m table. Eagerly batch-loaded
    # by resolvers; None means "not exposed for this path/role".
    apps_loaded: Private[Optional[List["App"]]] = None

    @strawberry.field
    def apps(
        self,
        owned: Optional[bool] = None,
        app_type: Optional[List[str]] = None,
        filter: Optional[AppFilter] = None,
    ) -> Optional[List["App"]]:
        """The apps this file carries, across the app_files m2m. Admin only, and only
        hydrated under the top-level `files` query."""
        if self.apps_loaded is None:
            return None
        return [a for a in self.apps_loaded if match_app(a, owned, filter, app_type)]


@strawberry.type
class AppVersion:
    """One known version of the content an app represents."""
    version: int
    owned: bool
    release_date: Optional[str] = None


@strawberry.type
class App:
    id: strawberry.ID
    title_id: str
    app_id: str
    app_version: str
    app_type: str
    owned: bool
    release_date: Optional[str] = None

    title: Optional["Title"] = strawberry.field(default=None, description=(
        "The parent title, so a card built from an app can show the title's name and "
        "ownership without a second round trip - and so a file list can name the game "
        "behind an UPDATE file, whose own titledb row usually does not exist. "
        "Hydrated by the `apps` and `files` queries. Null under `Title.apps`, where "
        "the parent already is the title, and for apps reached as a file's back-link "
        "under `apps { files { apps } }`."))

    versions: Optional[List[AppVersion]] = strawberry.field(default=None, description=(
        "Every version known for the content this app represents: for BASE apps that "
        "means the title's UPDATE apps (a Switch update ships under its own app id), "
        "for anything else the versions of this same app id. Ascending. Hydrated by "
        "the `apps`, `title`, `titles` and `files` queries; null for apps reached as a "
        "file's back-link under `apps { files { apps } }`."))

    # Eagerly batch-loaded by the apps/titles resolvers (admin only). None means
    # "not exposed for this role"; an empty list means "exposed but no files".
    files_loaded: Private[Optional[List[File]]] = None
    titledb_loaded: Private[Optional["Title"]] = None

    @strawberry.field
    def files(self, filter: Optional[FileFilter] = None) -> Optional[List[File]]:
        """The files that carry this app. Admin only - null for any other role, and
        null for apps reached as a file's back-link (the recursion stops there)."""
        if self.files_loaded is None:
            return None
        return [f for f in self.files_loaded if match_file(f, filter)]

    @strawberry.field
    def titledb(self) -> Optional["Title"]:
        """Titledb entry keyed by this app's app_id (the DLC's own metadata for
        DLC apps, the parent title's metadata for BASE apps, often null for
        UPDATE apps not present in titledb). Not hydrated for apps reached as a
        file's back-link under `apps { files { apps } }`."""
        return self.titledb_loaded


@strawberry.type
class Title:
    title_id: strawberry.ID
    source: str
    name: Optional[str] = None
    banner_url: Optional[str] = None
    icon_url: Optional[str] = None
    front_box_art: Optional[str] = None
    description: Optional[str] = None
    intro: Optional[str] = None
    developer: Optional[str] = None
    publisher: Optional[str] = None
    release_date: Optional[str] = None
    category: Optional[List[str]] = None
    is_demo: Optional[str] = None
    nsu_id: Optional[str] = None
    number_of_players: Optional[str] = None
    parent_id: Optional[str] = None
    rank: Optional[str] = None
    rating: Optional[str] = None
    rating_content: Optional[List[str]] = None
    region: Optional[str] = None
    regions: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    language: Optional[str] = None
    rights_id: Optional[str] = None
    screenshots: Optional[List[str]] = None
    size: Optional[str] = None
    version: Optional[str] = None
    nca_key: Optional[str] = None
    ids: Optional[List[str]] = None
    ownership: Optional[Ownership] = None

    # Eagerly batch-loaded by the titles resolver. None means "not exposed for
    # this role"; an empty list means "exposed but no apps". Hidden from the
    # schema via Private; clients access it through the apps() resolver below.
    apps_loaded: Private[Optional[List[App]]] = None

    @strawberry.field
    def apps(
        self,
        owned: Optional[bool] = None,
        app_type: Optional[List[str]] = None,
        filter: Optional[AppFilter] = None,
    ) -> Optional[List[App]]:
        if self.apps_loaded is None:
            return None
        return [a for a in self.apps_loaded if match_app(a, owned, filter, app_type)]


@strawberry.type
class TitleConnection:
    total: int
    items: List[Title]


@strawberry.type
class AppConnection:
    total: int
    items: List[App]


@strawberry.type
class FileConnection:
    total: int
    items: List[File]
