"""GraphQL schema assembly."""
from typing import List, Optional

import strawberry
from strawberry.extensions import QueryDepthLimiter
from strawberry.types import Info
from typing_extensions import Annotated

from .docs import arg as _arg, described, described_field
from .filters import AppFilter, AppType, FileFilter, OrderBy, TitleFilter
from .mutations import Mutation
from .resolvers import (
    resolve_app, resolve_apps, resolve_file, resolve_files, resolve_libraries,
    resolve_stats, resolve_task, resolve_tasks, resolve_title, resolve_titles,
    resolve_workers,
)
from .types import (
    App, AppConnection, File, FileConnection, Library, LibraryStats, Task, TaskStatus,
    Title, TitleConnection, Worker,
)


# Arguments that mean the same thing on several queries are annotated once here, so
# the docs pane says the same thing about `page` wherever a client meets it.
Page = Annotated[int, _arg(
    "1-based page number. Values below 1 are clamped rather than rejected.")]
TitlePageSize = Annotated[int, _arg(
    "Rows per page, clamped to 1-500.")]
PageSize = Annotated[int, _arg(
    "Rows per page, clamped to 1-1000.")]
Order = Annotated[Optional[OrderBy], _arg(
    "How to sort the page. Defaults to primary key order. A sort field this query "
    "has no column for degrades to that default instead of erroring.")]
Owned = Annotated[Optional[bool], _arg(
    "Restrict to owned or unowned rows. Omit for both. ANDs with `filter`, which "
    "carries the same predicate under a different spelling.")]
AppTypes = Annotated[Optional[List[AppType]], _arg(
    "Restrict to these kinds of content. A bare value is coerced to a one-element "
    "list; an empty list is no constraint. ANDs with `filter: {appType:}`, which asks "
    "for exactly one.")]


@described(strawberry.type)
class Query:
    """Read access to the library and the titledb catalogue.

    Every query is role-gated rather than hidden: a role that may not read something
    gets an empty page or a null, not an error. `titles` and `apps` need shop access;
    `files`, `libraries`, `tasks` and the file-level figures in `stats` need admin."""

    @described_field
    def title(
        self, info: Info,
        title_id: Annotated[strawberry.ID, _arg(
            "The 16-hex-digit title id, uppercase.")],
    ) -> Optional[Title]:
        """One title by id, whether it is owned or only in the catalogue. Null when no
        title carries that id. `ownership` is null for a catalogue-only title."""
        return resolve_title(str(title_id), info.context, info)

    @described_field
    def titles(
        self, info: Info,
        owned: Annotated[Optional[bool], _arg(
            "`true` lists what the library holds, including titles titledb does not "
            "recognize. `false` lists the catalogue minus the library - the "
            "'what could I add' view. Omit for everything.")] = None,
        filter: Annotated[Optional[TitleFilter], _arg(
            "Field-level predicates, ANDed together.")] = None,
        search: Annotated[Optional[str], _arg(
            "Free-text match across the title's name and its ids - the search box, "
            "as opposed to `filter`'s exact predicates.")] = None,
        order_by: Order = None,
        page: Page = 1,
        page_size: TitlePageSize = 50,
    ) -> TitleConnection:
        """A page of titles, from the catalogue and the library together. Requires
        shop access; returns an empty page otherwise."""
        return resolve_titles(
            owned=owned, filter=filter, search=search, order_by=order_by,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @described_field
    def apps(
        self, info: Info,
        owned: Owned = None,
        app_type: AppTypes = None,
        filter: Annotated[Optional[AppFilter], _arg(
            "Field-level predicates, ANDed together and with the shorthand "
            "arguments.")] = None,
        up_to_date: Annotated[Optional[bool], _arg(
            "Restrict to apps with nothing newer available. Title-level: it reads the "
            "title's flag for BASE apps and 'is the highest known version owned' for "
            "the rest.")] = None,
        complete: Annotated[Optional[bool], _arg(
            "Restrict to titles whose DLC set is complete. Only ever matches BASE "
            "apps, so combining it with `appType: [DLC]` matches nothing.")] = None,
        search: Annotated[Optional[str], _arg(
            "Free-text match across the parent title's name and either id.")] = None,
        order_by: Order = None,
        group_by_app_id: Annotated[bool, _arg(
            "Collapse an app id's versions into one item, so a page is a page of "
            "distinct apps rather than of (app id, version) rows - the card list.")]
            = False,
        page: Page = 1,
        page_size: PageSize = 100,
    ) -> AppConnection:
        """A page of apps: base games, updates and DLC, owned or merely known.
        Requires shop access; returns an empty page otherwise.

        `groupByAppId` collapses an app id's versions into one item, so a page is
        a page of distinct apps rather than of (app id, version) rows. The item is the
        group's highest-version row - a real app, not a blend of several - except for
        `owned`, which is group-level: true when any version is owned. `upToDate` and
        `complete` are title-level notions: `upToDate` asks the title's flag for BASE
        apps and "is the highest known version owned" for the others, while `complete`
        only ever matches BASE apps."""
        return resolve_apps(
            owned=owned, app_type=app_type, filter=filter,
            up_to_date=up_to_date, complete=complete,
            search=search, order_by=order_by, group_by_app_id=group_by_app_id,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @described_field
    def files(
        self, info: Info,
        filter: Annotated[Optional[FileFilter], _arg(
            "Field-level predicates, ANDed together.")] = None,
        order_by: Order = None,
        page: Page = 1,
        page_size: PageSize = 100,
    ) -> FileConnection:
        """A page of tracked files. Admin only - every other role gets an empty page,
        since file paths are filesystem layout.

        `orderBy: {field: ADDED_AT, direction: DESC}` is the "recently added" view;
        SIZE and DOWNLOAD_COUNT sort the obvious way. A field this query has no column
        for falls back to id order rather than erroring."""
        return resolve_files(
            filter=filter, order_by=order_by, page=page, page_size=page_size,
            ctx=info.context, info=info,
        )

    @described_field
    def app(
        self, info: Info,
        id: Annotated[strawberry.ID, _arg("Primary key of the app row.")],
    ) -> Optional[App]:
        """One app by primary key, including an item returned by
        `apps(groupByAppId: true)` - a grouped item is the group's highest-version row,
        not a composite, so its `id` resolves back to that same app."""
        return resolve_app(str(id), info.context, info)

    @described_field
    def file(
        self, info: Info,
        id: Annotated[strawberry.ID, _arg("Primary key of the file row.")],
    ) -> Optional[File]:
        """One file by primary key. Admin only - null for any other role, which is
        indistinguishable from the file not existing, deliberately."""
        return resolve_file(str(id), info.context, info)

    @described_field
    def libraries(self, info: Info) -> List[Library]:
        """The configured library roots. Admin only; empty for any other role."""
        return resolve_libraries(ctx=info.context, info=info)

    @described_field
    def tasks(
        self, info: Info,
        status: Annotated[Optional[TaskStatus], _arg(
            "Restrict to one lifecycle state. Omit for all of them.")] = None,
        task_name: Annotated[Optional[str], _arg(
            "Restrict to one registered task name, e.g. `scan_library`. A name no "
            "task registers is an error, not an empty list.")] = None,
        include_children: Annotated[bool, _arg(
            "Include sub-tasks as rows of their own. Off by default, so an activity "
            "list does not count the same work twice.")] = False,
        limit: Annotated[int, _arg(
            "Maximum rows to return, clamped to 1-500.")] = 50,
    ) -> List[Task]:
        """Background jobs, newest first. Admin only; empty for any other role.
        Top-level only unless `includeChildren`. An unregistered `taskName` is an
        error rather than an empty list, so a typo does not read as "nothing has
        run"."""
        return resolve_tasks(
            status=status, task_name=task_name, include_children=include_children,
            limit=limit, ctx=info.context, info=info,
        )

    @described_field
    def task(
        self, info: Info,
        id: Annotated[strawberry.ID, _arg("Primary key of the task row.")],
    ) -> Optional[Task]:
        """One task by id, with its children when selected. Admin only - null for any
        other role."""
        return resolve_task(str(id), info.context, info)

    @described_field
    def workers(self, info: Info) -> List[Worker]:
        """The task worker pool, ordered by worker number, each with the task it is
        running now. Admin only; empty for any other role, and empty in a process that
        does not own the pool."""
        return resolve_workers(ctx=info.context, info=info)

    @described_field
    def stats(self, info: Info) -> LibraryStats:
        """Library-wide aggregates for dashboards. Every field is computed only when
        selected, so asking for one count does not pay for the others. File-level
        figures are admin only and read zero for other roles."""
        return resolve_stats(ctx=info.context, info=info)


# The deepest legitimate query the UI issues is roughly
# files { items { apps { title { ownership { ... } } } } } - about 7 levels. 15 leaves
# generous headroom while still refusing the pathological nestings the hydration chain
# would otherwise happily expand, on an endpoint any shop-access user can reach.
MAX_QUERY_DEPTH = 15

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[QueryDepthLimiter(max_depth=MAX_QUERY_DEPTH)],
)
