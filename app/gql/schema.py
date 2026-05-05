"""GraphQL schema assembly."""
from typing import Optional

import strawberry
from strawberry.types import Info

from .filters import AppFilter, FileFilter, TitleFilter
from .resolvers import (
    resolve_apps, resolve_files, resolve_title, resolve_titles,
)
from .types import AppConnection, FileConnection, Title, TitleConnection


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
        page: int = 1,
        page_size: int = 50,
    ) -> TitleConnection:
        return resolve_titles(
            owned=owned, filter=filter,
            page=page, page_size=page_size, ctx=info.context, info=info,
        )

    @strawberry.field
    def apps(
        self, info: Info,
        owned: Optional[bool] = None,
        filter: Optional[AppFilter] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> AppConnection:
        return resolve_apps(
            owned=owned, filter=filter,
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
