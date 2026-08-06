"""GraphQL schema assembly."""
from enum import Enum
from typing import List, Optional

import strawberry
from strawberry.types import Info

from .filters import AppFilter, FileFilter, TitleFilter
from .resolvers import (
    resolve_apps, resolve_files, resolve_title, resolve_titles,
)
from .types import AppConnection, FileConnection, Title, TitleConnection


@strawberry.enum
class OrderBy(Enum):
    """Sort order for the paginated queries. ID is the id order rows come back in
    naturally; NAME sorts by title name, unrecognized titles last."""
    ID = "id"
    NAME = "name"


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
        order_by: OrderBy = OrderBy.ID,
        page: int = 1,
        page_size: int = 50,
    ) -> TitleConnection:
        return resolve_titles(
            owned=owned, filter=filter, search=search, order_by=order_by.value,
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
        order_by: OrderBy = OrderBy.ID,
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
            search=search, order_by=order_by.value, group_by_app_id=group_by_app_id,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @strawberry.field
    def files(
        self, info: Info,
        filter: Optional[FileFilter] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> FileConnection:
        return resolve_files(
            filter=filter, page=page, page_size=page_size, ctx=info.context, info=info,
        )


schema = strawberry.Schema(query=Query)
