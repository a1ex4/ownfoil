"""Strawberry GraphQL types for ownfoil.

Batch-loaded fields use `null` to mean "not loaded on this path, or not exposed to
this role" and `[]` to mean "loaded, and there is nothing". The two are not the same
thing, and no client can tell them apart from the value alone - so every such field
says in its description which paths hydrate it.
"""
import json
from enum import Enum

import strawberry
from strawberry import Private
from typing import List, Optional
from typing_extensions import Annotated

from db import verification_status

from .docs import arg, desc, described, described_field
from .filters import AppFilter, FileFilter, VerificationStatus, match_app, match_file
from .scalars import BigInt

# Arguments shared by the nested list fields, annotated once so `Title.apps` and
# `File.apps` describe them identically.
NestedAppTypes = Annotated[Optional[List[str]], arg(
    "Restrict to these app types (BASE, UPDATE, DLC). A bare string is coerced to a "
    "one-element list; an empty list is no constraint.")]
NestedAppFilter = Annotated[Optional[AppFilter], arg(
    "Field-level predicates, applied in memory to the already-loaded list. Same "
    "semantics as the SQL the top-level `apps` query runs.")]
NestedFileFilter = Annotated[Optional[FileFilter], arg(
    "Field-level predicates, applied in memory to the already-loaded list. Same "
    "semantics as the SQL the top-level `files` query runs.")]


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


@described(strawberry.type)
class Ownership:
    """What the library holds for a title. Null on a title the library has never
    seen - which is a different statement from all three flags being false."""
    have_base: bool = desc("The base game is in the library. A title can own DLC or "
                           "updates without this, if the base file was never added.")
    up_to_date: bool = desc("No newer UPDATE version is known than the highest one "
                            "owned. False also when nothing is owned at all.")
    complete: bool = desc("Every DLC titledb attributes to this title is owned.")


@described(strawberry.type)
class Library:
    """A configured library root. Admin only - the path is filesystem layout."""
    id: strawberry.ID = desc("Primary key of the library row.")
    path: str = desc("Absolute path of the library root on the server's filesystem.")
    last_scan: Optional[str] = desc(
        "When this root was last scanned, ISO 8601. Null if it never has been.",
        default=None)


@described(strawberry.enum)
class TaskStatus(Enum):
    """Every state a task row can be in. Cancelling deletes the row rather than
    adding a terminal state, so there is no CANCELLED member."""
    PENDING = strawberry.enum_value(
        "pending", description="Queued, not yet picked up by a worker.")
    RUNNING = strawberry.enum_value(
        "running", description="Claimed by a worker and executing now.")
    WAITING_FOR_CHILDREN = strawberry.enum_value(
        "waiting_for_children",
        description="Its own work is done; it stays open until its sub-tasks finish.")
    COMPLETED = strawberry.enum_value(
        "completed", description="Finished successfully.")
    FAILED = strawberry.enum_value(
        "failed", description="Raised, or was interrupted by a worker restart. "
                              "Failed tasks persist until purged by hand.")


@described(strawberry.type)
class Task:
    """A queued, running or finished background job."""
    id: strawberry.ID = desc("Primary key of the task row.")
    task_name: str = desc("Which registered task this is, e.g. `scan_library`. The "
                          "name determines the shape of `input`.")
    status: TaskStatus = desc("Where the task is in its lifecycle.")
    completion_pct: int = desc("Progress, 0-100. Only tasks that report progress "
                               "move it off 0 before finishing.", default=0)
    exit_code: Optional[int] = desc(
        "0 on success, non-zero on failure. Null while the task has not finished.",
        default=None)
    error_message: Optional[str] = desc(
        "Why the task failed. Null unless `status` is FAILED.", default=None)
    created_at: Optional[str] = desc("When the task was enqueued, ISO 8601.",
                                     default=None)
    started_at: Optional[str] = desc(
        "When a worker claimed it, ISO 8601. Null while PENDING.", default=None)
    completed_at: Optional[str] = desc(
        "When it reached a terminal state, ISO 8601. Null until then.", default=None)
    run_after: Optional[str] = desc(
        "Earliest time this task may start, ISO 8601 - set on work that is "
        "deliberately deferred. Null for tasks eligible immediately.", default=None)
    parent_id: Optional[strawberry.ID] = desc(
        "The task that spawned this one. Null for top-level tasks, which are the "
        "only ones `tasks` returns unless `includeChildren` is set.", default=None)
    input: Optional[str] = desc(
        "The task's arguments, as a JSON object string. JSON rather than a typed "
        "field because the payload shape differs per task name, and the schema "
        "cannot describe a union of every registered task's input.", default=None)
    output: Optional[str] = desc(
        "Whatever the task recorded on finishing, as a JSON string. Null until it "
        "finishes, and for tasks that report nothing.", default=None)

    children_loaded: Private[Optional[List["Task"]]] = None

    @described_field
    def children(self) -> Optional[List["Task"]]:
        """Sub-tasks spawned by this one, e.g. the per-library `scan_library` jobs
        under a `scan_libraries`. Only hydrated when selected; null otherwise."""
        return self.children_loaded


@described(strawberry.type)
class CountByKey:
    """One bucket of a grouped count."""
    key: str = desc("The value grouped on, e.g. an app type.")
    count: int = desc("How many rows fell into this bucket.")


@described(strawberry.type)
class SizedCountByKey:
    """A bucket whose rows are files, so the bytes they account for are meaningful."""
    key: str = desc("The value grouped on, e.g. a file extension or a library path.")
    count: int = desc("How many files fell into this bucket.")
    size: BigInt = desc("Total bytes of the files in this bucket.", default=0)


@described(strawberry.type)
class VerificationStatusCount:
    """Files sharing one verification verdict. Typed rather than a `SizedCountByKey`
    so the bucket can be handed straight back to `files(filter: {verificationStatus:})`
    without a string round-trip."""
    status: VerificationStatus = desc("The verdict this bucket groups on.")
    count: int = desc("How many files carry it.")
    size: BigInt = desc("Total bytes of the files in this bucket.", default=0)


@described(strawberry.type)
class LibraryStats:
    """Library-wide aggregates, for dashboards. Each field is computed only when
    selected, so asking for one count does not pay for the others. The file-level
    figures are admin only and read 0 for any other role."""
    total_files: int = desc("Files tracked in the library. Admin only.", default=0)
    total_size: BigInt = desc("Total bytes of every tracked file. Admin only.",
                              default=0)
    identified_files: int = desc(
        "Files matched to an app. Admin only.", default=0)
    unidentified_files: int = desc(
        "`totalFiles` minus `identifiedFiles` - files ownfoil could not match, which "
        "is the queue of things needing attention. Admin only.", default=0)
    total_titles: int = desc(
        "Titles in the titledb catalogue - the whole known universe of games, not "
        "the ones owned. Compare `ownedTitles`.", default=0)
    owned_titles: int = desc(
        "Titles whose base game is in the library.", default=0)
    total_apps: int = desc(
        "App rows, owned or not: every base, update and DLC ownfoil knows about for "
        "the titles it tracks.", default=0)
    owned_apps: int = desc("App rows backed by at least one file.", default=0)
    files_by_extension: Optional[List[SizedCountByKey]] = desc(
        "File count and bytes per extension, most files first. Admin only; null for "
        "any other role.", default=None)
    apps_by_type: Optional[List[CountByKey]] = desc(
        "App count per type (BASE/UPDATE/DLC). No byte totals: apps are metadata "
        "rows, and the files behind them are counted by `filesByExtension`.",
        default=None)
    files_by_library: Optional[List[SizedCountByKey]] = desc(
        "File count and bytes per configured library root, keyed by path. Includes "
        "roots holding nothing. Admin only; null for any other role.", default=None)
    files_by_verification_status: Optional[List[VerificationStatusCount]] = desc(
        "File count and bytes per `File.verificationStatus`, best verdict first and "
        "`UNVERIFIED` last. Always all seven buckets, empty ones included - the set is "
        "closed, and `CORRUPT: 0` says something a missing bucket does not. Note "
        "`SIGNATURE_OK` and `SIGNATURE_FAILED` can only be non-zero while verification "
        "runs at `signature` depth. Admin only; null for any other role.", default=None)


@described(strawberry.type)
class File:
    """One file on disk that ownfoil tracks. The whole type is admin only - the
    `files` query returns nothing at all for any other role."""
    id: strawberry.ID = desc("Primary key of the file row.")
    library_id: int = desc("Which configured library root this file sits under. "
                           "`library` resolves the root itself.")
    filename: str = desc("File name with extension, no directory part.")
    folder: Optional[str] = desc(
        "Directory holding the file, relative to its library root. Null for a file "
        "sitting at the root itself.", default=None)
    extension: Optional[str] = desc(
        "Lowercase extension without the dot, e.g. `nsp`, `nsz`.", default=None)
    size: Optional[BigInt] = desc(
        "Size in bytes. A 64-bit scalar, because game files routinely exceed the "
        "2^31 a GraphQL `Int` can carry.", default=None)
    compressed: bool = desc(
        "The file is in a compressed container (NSZ/XCZ) rather than NSP/XCI.",
        default=False)
    multicontent: bool = desc(
        "The file carries more than one app - a bundle of base plus updates or DLC. "
        "`apps` lists them and `nbContent` counts them.", default=False)
    nb_content: int = desc("How many apps this file carries; 1 for an ordinary file.",
                           default=0)
    download_count: int = desc("How many times a shop client has downloaded it.",
                               default=0)
    identified: bool = desc(
        "Ownfoil worked out which app(s) this file contains. Unidentified files stay "
        "in the library but cannot be served meaningfully.", default=False)
    identification_type: Optional[str] = desc(
        "How the file was identified - by reading the file's own metadata, or by "
        "parsing its name. Null while unidentified.", default=None)
    identification_error: Optional[str] = desc(
        "Why identification failed, if it did. Null otherwise.", default=None)
    identification_attempts: int = desc(
        "How many times identification has been tried. Rises only on failure; a "
        "large value means a file that keeps refusing to identify.", default=0)
    organized: bool = desc(
        "The file sits where the configured naming template says it should. False "
        "means a re-organize would move or rename it.", default=False)
    signature_valid: Optional[bool] = desc(
        "Whether every NCA header signature checked out, and the container decrypted "
        "with the configured keys. This is a provenance check, not an integrity one: "
        "`false` means re-signed, which a repack commonly is, and says nothing about "
        "whether the contents are intact. Null means never verified.", default=None)
    hash_valid: Optional[bool] = desc(
        "Whether every NCA's content hashed to what its name and CNMT claim - the "
        "actual integrity verdict, and the one that blocks compression. Null unless "
        "verification ran at `hash` depth, which reads the whole file.", default=None)
    hash_modified: Optional[bool] = desc(
        "Splits a `hashValid: false` in two. `true` means the failing contents are still "
        "filed under exactly the names the container's CNMT records, so they were "
        "rewritten in place rather than damaged or swapped. Null when the contents were "
        "never hashed, and on rows verified before this was recorded.", default=None)
    verification_error: Optional[str] = desc(
        "What verification objected to, if anything. Null when the file passed or was "
        "never checked.", default=None)
    verified_at: Optional[str] = desc(
        "When verification last ran. The verdicts describe the bytes as of then; a "
        "change on disk clears all four fields.", default=None)
    mtime: Optional[float] = desc(
        "Filesystem modification time, Unix epoch seconds. Rewritten by a copy or a "
        "re-organize, so it is not a reliable 'when did I get this'.", default=None)
    added_at: Optional[str] = desc(
        "When ownfoil first saw this file. Distinct from `mtime`, which a re-organize "
        "or a copy rewrites - sort by this for a 'recently added' view.", default=None)
    filepath: Optional[str] = desc(
        "Absolute path on the server. Admin only - null for any other role, since it "
        "exposes filesystem layout.", default=None)

    # Apps linked to this file via the app_files m2m table. Eagerly batch-loaded
    # by resolvers; None means "not exposed for this path/role".
    apps_loaded: Private[Optional[List["App"]]] = None
    library_loaded: Private[Optional[Library]] = None

    @described_field
    def verification_status(self) -> VerificationStatus:
        """`signatureValid`, `hashValid` and `hashModified` read as one label - which of
        them matters depends on the others, and a client that reasons about them
        separately gets it wrong. Computed from those three fields, so it costs no extra
        query and is never stale. `filter: {verificationStatus:}` selects on the same
        rule."""
        return VerificationStatus(verification_status(self))

    @described_field
    def library(self) -> Optional[Library]:
        """The library root this file was found under. Admin only."""
        return self.library_loaded

    @described_field
    def apps(
        self,
        app_type: NestedAppTypes = None,
        filter: NestedAppFilter = None,
    ) -> Optional[List["App"]]:
        """The apps this file carries, across the app_files m2m. Usually one; more for
        a multicontent bundle. Admin only, and only hydrated under the top-level
        `files` query.

        Unlike `Title.apps` this takes no `owned` argument: an app is owned exactly
        when it has files, so everything reachable from a file is owned by
        construction and the filter could only ever return all of them or none.
        `filter: {owned: ...}` is still accepted here - the input type is shared with
        `Title.apps` - and is subject to the same tautology."""
        if self.apps_loaded is None:
            return None
        return [a for a in self.apps_loaded if match_app(a, None, filter, app_type)]


@described(strawberry.type)
class AppVersion:
    """One known version of the content an app represents, with ownership attached."""
    version: int = desc("The version number, as Nintendo counts them: 0 for a launch "
                        "release, then multiples of 65536.")
    owned: bool = desc("This exact version is in the library.")
    release_date: Optional[str] = desc(
        "When this version shipped, as titledb reports it. Null when unknown.",
        default=None)


@described(strawberry.type)
class TitledbVersion:
    """A version titledb knows about, with no ownership attached - this is catalogue
    data, and it exists for titles the library has never seen."""
    version: int = desc("The version number, as Nintendo counts them.")
    release_date: Optional[str] = desc(
        "When this version shipped. Null when titledb does not say.", default=None)


@described(strawberry.type)
class TitledbDlc:
    """A DLC titledb attributes to a title, again independent of ownership."""
    app_id: str = desc("The DLC's own 16-hex-digit application id.")
    version: Optional[int] = desc(
        "Highest version titledb knows for this DLC. Null when it says nothing.",
        default=None)
    titledb: Optional["Title"] = desc(
        "The DLC's own catalogue metadata - its name and art, which is what a "
        "'missing DLC' list needs to show. Null when titledb has no entry for it.",
        default=None)


@described(strawberry.type)
class App:
    """One piece of installable content: a base game, an update, or a DLC. An app is
    a (application id, version) pair, and it is `owned` exactly when a file carries
    it."""
    id: strawberry.ID = desc("Primary key of the app row.")
    title_id: str = desc("The 16-hex-digit id of the title this belongs to, "
                         "uppercase. For a DLC or update this differs from `appId`.")
    app_id: str = desc("This content's own 16-hex-digit application id. Equal to "
                       "`titleId` for a base game; derived from it for updates and DLC.")
    app_version: int = desc(
        "The content version, as an integer - the same quantity `AppVersion.version` "
        "reports. Stored as text in the database, so it is cast on the way out; a "
        "non-numeric value reads as 0, matching what SQL comparisons on it do.")
    app_type: str = desc("BASE, UPDATE or DLC.")
    owned: bool = desc(
        "At least one file in the library carries this app. Under "
        "`apps(groupByAppId: true)` this is group-level: true when any version of the "
        "app id is owned, even though the item shown is the highest version.")
    release_date: Optional[str] = desc(
        "When this version shipped. Populated for UPDATE rows; null on BASE and DLC "
        "rows, whose date lives on `titledb.releaseDate`.", default=None)

    title: Optional["Title"] = desc(
        "The parent title, so a card built from an app can show the title's name and "
        "ownership without a second round trip - and so a file list can name the game "
        "behind an UPDATE file, whose own titledb row usually does not exist. "
        "Hydrated by the `apps` and `files` queries. Null under `Title.apps`, where "
        "the parent already is the title, and for apps reached as a file's back-link "
        "under `apps { files { apps } }`.", default=None)

    versions: Optional[List[AppVersion]] = desc(
        "Every version known for the content this app represents: for BASE apps that "
        "means the title's UPDATE apps (a Switch update ships under its own app id), "
        "for anything else the versions of this same app id. Ascending. Hydrated by "
        "the `apps`, `title`, `titles` and `files` queries; null for apps reached as a "
        "file's back-link under `apps { files { apps } }`.", default=None)

    # Eagerly batch-loaded by the apps/titles resolvers (admin only). None means
    # "not exposed for this role"; an empty list means "exposed but no files".
    files_loaded: Private[Optional[List[File]]] = None
    titledb_loaded: Private[Optional["Title"]] = None

    @described_field
    def files(self, filter: NestedFileFilter = None) -> Optional[List[File]]:
        """The files that carry this app - more than one when the same content sits in
        the library twice. Admin only: null for any other role, and null for apps
        reached as a file's back-link (the recursion stops there)."""
        if self.files_loaded is None:
            return None
        return [f for f in self.files_loaded if match_file(f, filter)]

    @described_field
    def titledb(self) -> Optional["Title"]:
        """Titledb entry keyed by this app's app_id (the DLC's own metadata for
        DLC apps, the parent title's metadata for BASE apps, often null for
        UPDATE apps not present in titledb). Not hydrated for apps reached as a
        file's back-link under `apps { files { apps } }`."""
        return self.titledb_loaded


@described(strawberry.type)
class Title:
    """A game, as the titledb catalogue describes it - which exists whether or not
    anything is owned. Everything from `name` down is metadata passed through from
    titledb, merged across its sources at import time; ownership lives on
    `ownership` and `apps`."""
    title_id: strawberry.ID = desc("The 16-hex-digit title id, uppercase.")
    source: str = desc(
        "Which metadata source won for this title: `titledb` for the downloaded "
        "catalogue, `custom` when a local override supplies the values.")
    name: Optional[str] = desc("The game's name. Null for a title no source names.",
                               default=None)
    banner_url: Optional[str] = desc("URL of the wide banner artwork.", default=None)
    icon_url: Optional[str] = desc("URL of the square icon artwork.", default=None)
    front_box_art: Optional[str] = desc("URL of the box art.", default=None)
    description: Optional[str] = desc("Long-form store description.", default=None)
    intro: Optional[str] = desc("Short tagline, where the store has one.", default=None)
    developer: Optional[str] = desc("Studio that made the game.", default=None)
    publisher: Optional[str] = desc("Publisher of record.", default=None)
    release_date: Optional[str] = desc(
        "Original release date as titledb reports it - the format is the catalogue's, "
        "not a normalized one, so sort with `orderBy: {field: RELEASE_DATE}` rather "
        "than parsing it.", default=None)
    category: Optional[List[str]] = desc(
        "Genre tags. Stored JSON-encoded, so `TitleFilter.category` takes a "
        "`StringListFilter` whose operators name elements.", default=None)
    is_demo: Optional[str] = desc(
        "Whether the entry is a demo, as titledb spells it - a string, not a boolean, "
        "because the catalogue is not consistent about it.", default=None)
    nsu_id: Optional[str] = desc("Nintendo eShop identifier, where one is known.",
                                 default=None)
    number_of_players: Optional[str] = desc(
        "Supported player count, as titledb spells it.", default=None)
    parent_id: Optional[str] = desc(
        "Title id this entry belongs under, for regional variants that share a game.",
        default=None)
    rank: Optional[str] = desc("Store popularity rank, where the catalogue has one.",
                              default=None)
    rating: Optional[str] = desc("Age rating value for `region`.", default=None)
    rating_content: Optional[List[str]] = desc(
        "Content descriptors behind the rating, e.g. violence.", default=None)
    region: Optional[str] = desc("Primary region this entry describes.", default=None)
    regions: Optional[List[str]] = desc("Every region the title released in.",
                                        default=None)
    languages: Optional[List[str]] = desc("Languages the title supports.",
                                          default=None)
    language: Optional[str] = desc("Primary language of this catalogue entry.",
                                   default=None)
    rights_id: Optional[str] = desc("Rights id, as used by the content's DRM.",
                                    default=None)
    screenshots: Optional[List[str]] = desc("URLs of store screenshots.", default=None)
    size: Optional[str] = desc(
        "Install size as titledb reports it - a string, unlike `File.size`, because "
        "the catalogue value is not reliably numeric. Sorting by SIZE casts it.",
        default=None)
    version: Optional[str] = desc(
        "Latest version titledb records for the title. `availableVersions` is the "
        "full list.", default=None)
    nca_key: Optional[str] = desc(
        "Title key from the catalogue (`key` upstream), where present.", default=None)
    ids: Optional[List[str]] = desc(
        "Every application id the catalogue associates with this title.", default=None)
    ownership: Optional[Ownership] = desc(
        "What the library holds for this title. Null for a title that is only in the "
        "catalogue - which is why it is not three booleans on this type.", default=None)

    # Eagerly batch-loaded by the titles resolver. None means "not exposed for
    # this role"; an empty list means "exposed but no apps". Hidden from the
    # schema via Private; clients access it through the apps() resolver below.
    apps_loaded: Private[Optional[List[App]]] = None
    available_versions_loaded: Private[Optional[List[TitledbVersion]]] = None
    available_dlc_loaded: Private[Optional[List[TitledbDlc]]] = None

    @described_field
    def available_versions(self) -> Optional[List[TitledbVersion]]:
        """Every version titledb knows for this title, ascending. Unlike
        `apps { versions }` this does not need the title to be in the library, so a
        catalogue or wishlist view can show what a title would involve owning."""
        return self.available_versions_loaded

    @described_field
    def available_dlc(self) -> Optional[List[TitledbDlc]]:
        """Every DLC titledb attributes to this title, whether or not it is owned -
        the answer to "what am I missing" for a title the library has never seen."""
        return self.available_dlc_loaded

    @described_field
    def apps(
        self,
        owned: Annotated[Optional[bool], arg(
            "Restrict to owned or unowned apps. Omit for both - which is what makes "
            "'what am I missing' answerable.")] = None,
        app_type: NestedAppTypes = None,
        filter: NestedAppFilter = None,
    ) -> Optional[List[App]]:
        """The base, update and DLC apps ownfoil tracks for this title - including
        unowned ones, which is what makes "what am I missing" answerable. Null when
        not hydrated on this path or not exposed to this role."""
        if self.apps_loaded is None:
            return None
        return [a for a in self.apps_loaded if match_app(a, owned, filter, app_type)]


@described(strawberry.type)
class TitleConnection:
    """One page of titles."""
    total: int = desc("Titles matching the query across every page, before paging. "
                      "Computed only when selected.")
    items: List[Title] = desc("The titles on the requested page.")


@described(strawberry.type)
class AppConnection:
    """One page of apps."""
    total: int = desc("Apps matching the query across every page, before paging - "
                      "distinct app ids under `groupByAppId: true`. Computed only "
                      "when selected.")
    items: List[App] = desc("The apps on the requested page.")


@described(strawberry.type)
class FileConnection:
    """One page of files."""
    total: int = desc("Files matching the query across every page, before paging. "
                      "Computed only when selected.")
    items: List[File] = desc("The files on the requested page.")
