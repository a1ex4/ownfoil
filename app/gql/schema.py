"""GraphQL schema assembly."""
from typing import List, Optional

import strawberry
from strawberry.extensions import QueryDepthLimiter
from strawberry.types import Info

from .filters import AppFilter, FileFilter, OrderBy, TitleFilter
from .mutations import Mutation
from .resolvers import (
    resolve_app, resolve_apps, resolve_file, resolve_files, resolve_libraries,
    resolve_stats, resolve_task, resolve_tasks, resolve_title, resolve_titles,
)
from .types import (
    App, AppConnection, File, FileConnection, Library, LibraryStats, Task, Title,
    TitleConnection,
)


@strawberry.type
class Query:

    @strawberry.field
    def title(self, info: Info, title_id: strawberry.ID) -> Optional[Title]:
        return resolve_title(str(title_id), info.context, info)

    @strawberry.field
    def titles(
        self, info: Info,
        owned: Optional[bool] = None,
        filter: Optional[TitleFilter] = None,
        search: Optional[str] = None,
        order_by: Optional[OrderBy] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> TitleConnection:
        return resolve_titles(
            owned=owned, filter=filter, search=search, order_by=order_by,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @strawberry.field
    def apps(
        self, info: Info,
        owned: Optional[bool] = None,
        app_type: Optional[List[str]] = None,
        filter: Optional[AppFilter] = None,
        up_to_date: Optional[bool] = None,
        complete: Optional[bool] = None,
        search: Optional[str] = None,
        order_by: Optional[OrderBy] = None,
        group_by_app_id: bool = False,
        page: int = 1,
        page_size: int = 100,
    ) -> AppConnection:
        """`groupByAppId` collapses an app id's versions into one item, so a page is
        a page of distinct apps rather than of (app id, version) rows. `upToDate` and
        `complete` are title-level notions: `upToDate` asks the title's flag for BASE
        apps and "is the highest known version owned" for the others, while `complete`
        only ever matches BASE apps."""
        return resolve_apps(
            owned=owned, app_type=app_type, filter=filter,
            up_to_date=up_to_date, complete=complete,
            search=search, order_by=order_by, group_by_app_id=group_by_app_id,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @strawberry.field
    def files(
        self, info: Info,
        filter: Optional[FileFilter] = None,
        order_by: Optional[OrderBy] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> FileConnection:
        """`orderBy: {field: ADDED_AT, direction: DESC}` is the "recently added" view;
        SIZE and DOWNLOAD_COUNT sort the obvious way. A field this query has no column
        for falls back to id order rather than erroring."""
        return resolve_files(
            filter=filter, order_by=order_by, page=page, page_size=page_size,
            ctx=info.context, info=info,
        )

    @strawberry.field
    def app(self, info: Info, id: strawberry.ID) -> Optional[App]:
        """One app by primary key. Note that under `apps(groupByAppId: true)` an item's
        `id` is the lowest id of the group, not a stable identity - key on `appId`."""
        return resolve_app(str(id), info.context, info)

    @strawberry.field
    def file(self, info: Info, id: strawberry.ID) -> Optional[File]:
        return resolve_file(str(id), info.context, info)

    @strawberry.field
    def libraries(self, info: Info) -> List[Library]:
        """The configured library roots. Admin only; empty for any other role."""
        return resolve_libraries(ctx=info.context, info=info)

    @strawberry.field
    def tasks(
        self, info: Info,
        status: Optional[str] = None,
        task_name: Optional[str] = None,
        include_children: bool = False,
        limit: int = 50,
    ) -> List[Task]:
        """Background jobs, newest first. Top-level only unless `includeChildren`."""
        return resolve_tasks(
            status=status, task_name=task_name, include_children=include_children,
            limit=limit, ctx=info.context, info=info,
        )

    @strawberry.field
    def task(self, info: Info, id: strawberry.ID) -> Optional[Task]:
        return resolve_task(str(id), info.context, info)

    @strawberry.field
    def stats(self, info: Info) -> LibraryStats:
        """Library-wide aggregates for dashboards. File-level figures are admin only
        and read zero for other roles."""
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
