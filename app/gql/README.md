# GraphQL endpoint (`/api/graphql`)

This package implements the GraphQL API used by the ownfoil web UI to query the
Switch library. It replaces the old REST `/api/titles` plus the `library.json`
disk cache: the schema lets clients navigate freely between titles, apps, files,
and titledb metadata, and ETag revalidation gives the same "free reload" UX
without a precomputed artifact on disk.

## Terminology

The rest of this document leans on the following terms.

- **Resolver.** A function the GraphQL server calls to produce a field's value.
  In Strawberry, that's any `@strawberry.field` method or any callable
  registered as a field on a type. Top-level entry points (`title`, `titles`,
  `apps`, `files`) are resolvers on the `Query` type; nested fields like
  `Title.apps` also have resolver methods.
- **Selection / selection set.** The tree of fields a client asked for in a
  particular query. `{titles { items { name } } }` is a selection set with
  three nested levels. The server can introspect it via `info.selected_fields`
  and decide what work to do based on what was actually requested.
- **Hydration.** Loading the data needed to answer a request into in-memory
  objects so resolvers have something to return. In this codebase the parent
  resolver for a paginated list does the SQL work upfront and stuffs the
  results into `Private` dataclass slots on each child (`apps_loaded`,
  `files_loaded`, `titledb_loaded`); the child's `@strawberry.field` resolver
  then just reads from that slot. "Hydrate apps for these titles" means "run
  the SELECT, build `App` objects, attach them to each parent `Title`".
- **Batch loading / batched query.** One SELECT for many parents at once,
  using `WHERE x IN (...)`. The opposite of issuing one SELECT per parent.
  Every `_hydrate_*` / `_load_*` helper in `resolvers.py` is batched.
- **Eager loading.** Loading related data in the parent resolver before
  Strawberry walks into the children, instead of letting each child's resolver
  fire its own query. Avoids the N+1 pattern.
- **N+1.** The anti-pattern of running 1 query for the parent list, then N
  queries (one per item) for the related child data. Bad because the round-trip
  count scales with page size. The hydration chain exists specifically to keep
  the round-trip count constant per page.
- **Connection type.** A wrapper around a paginated list. Here that's
  `TitleConnection`/`AppConnection`/`FileConnection`, each with `{total, items}`.
  Both fields are independently selectable — the resolver runs the COUNT(*)
  only if `total` was selected, and the page query only if `items` was selected.
- **Field-level gate.** An auth check inside a resolver that returns `None`
  rather than raising. The schema still exposes the field; the data just
  doesn't surface for unauthorized roles. Contrast with view-level auth which
  rejects the whole request.
- **Introspection.** Clients can query the schema's own structure
  (`__schema`, `__type`) to discover fields, types, and arguments. Strawberry
  exposes this by default, so even fields a role can't actually read are
  *visible* to it.
- **Fragment / inline fragment / fragment spread.** GraphQL syntax for reusable
  or conditional selection sets. Strawberry's `info.selected_fields` resolves
  these for us, so `selection.py` can flatten everything into a uniform list of
  `SelectedField` nodes without caring whether they came from a fragment.

## Modules

| File | Role |
|---|---|
| `view.py` | Flask view + `/api/graphql` dispatch. Auth gate, ETag/304 fast path. |
| `schema.py` | Strawberry `Query` type. Wires field arguments to the resolvers. |
| `resolvers.py` | All SQL, all hydration. Builds `Title`/`App`/`File` instances for Strawberry to serialize. |
| `types.py` | Strawberry types (`Title`, `App`, `File`, `Ownership`) plus the `*Connection` paginated wrappers. |
| `filters.py` | Filter input types (`StringFilter`, etc.), SQL clause builders, and in-memory matchers used for nested-field filtering. |
| `selection.py` | Wraps `info.selected_fields` so resolvers can ask "did the client request this field?" Drives the selection-aware skip/project logic. |
| `scalars.py` | `BigInt` — required because GraphQL `Int` is 32-bit and Switch files exceed 2 GB. |
| `context.py` | Per-request `GraphQLContext` (user, `can_admin`, `can_shop`). |
| `cache.py` | World hash + ETag computation. |

## Top-level queries

| Query | Returns | Role |
|---|---|---|
| `title(titleId:)` | one `Title`, owned or catalogue-only | shop |
| `titles(owned:, filter:, search:, orderBy:, page:)` | `TitleConnection` | shop |
| `apps(owned:, appType:, upToDate:, complete:, groupByAppId:, …)` | `AppConnection` | shop |
| `app(id:)` | one `App` by primary key | shop |
| `files(filter:, page:)` | `FileConnection` | admin |
| `file(id:)` | one `File` by primary key | admin |
| `libraries` | the configured library roots | admin |
| `tasks(status:, taskName:, includeChildren:, limit:)` | background jobs, newest first | admin |
| `task(id:)` | one `Task` with its children | admin |
| `stats` | library-wide aggregates for dashboards | shop (file figures admin) |

`app(id:)` and `file(id:)` delegate to the list resolvers with a primary-key
filter, so every nested field hydrates exactly as it does under `apps` / `files`.
They pass `only_pk`, which also tells the resolver its selection set is the
item's own fields rather than a connection's `{total, items}`.

## Data sources

The endpoint reads two SQLite databases. Both are exposed on the same SQLAlchemy
session via SQLite `ATTACH`, managed by engine listeners in `app/db.py`:

- **`config/ownfoil.db`** — the main app DB, exposed as the default `main`
  schema. Owns `titles`, `apps`, `files`, `app_files`, plus auth and tasks.
  Everything actually in the user's library lives here.
- **`config/titles.db`** — the titledb metadata catalog (Nintendo Switch titles,
  versions, cnmts). Rebuilt periodically by `update_titledb` from the downloaded
  JSON files, in `app/titledb/`. Read-only from the GraphQL path's perspective.
  Exposed as the `titledb` schema. `titledb.titles` holds one row per title id,
  already merged across the metadata sources (`custom` > `extract` > `titledb`,
  field by field) by `app/titledb/store.py`, so resolvers join it plainly;
  `td.source` names the highest-priority source that contributed a field and
  `td.sources` lists every source that did.

The ATTACH is automatic, so resolvers just write `JOIN titledb.titles td ON ...`
or `FROM main.titles ot ...` and SQLAlchemy/SQLite handle the rest. The attach
also re-fires when titles.db is replaced atomically by another process — see the
`Engine.checkout` listener in `app/db.py`.

### titledb id case asymmetry

A trap worth flagging once, and it is not uniform across the tables:

| Table / column | Case | Why |
|---|---|---|
| `titledb.titles.id` | UPPER | normalised on import |
| `titledb.versions.title_id` | UPPER | normalised on import (`store._import_versions`) |
| `titledb.cnmts.app_id`, `.other_application_id` | as the JSON had them (lower) | inserted verbatim |

So joining `main.titles` (uppercase) to `titledb.titles` or `titledb.versions` is
direct; only `cnmts` needs normalising, on both sides — see
`_hydrate_titledb_dlc`. Getting this wrong returns an empty list rather than an
error, which is why `tests/test_gql_catalogue.py` pins it.

## Request flow

```
HTTP request
   │
   ▼
view.graphql_dispatch
   │   - auth (shop/admin or 401/403)
   │   - parse query/variables/operationName
   │   - compute ETag from (query, variables, role, world_hash)
   │   - return 304 if If-None-Match matches
   │
   ▼
schema.Query.{title,titles,apps,files}     ◄── Strawberry dispatches by field
   │
   ▼
resolvers.resolve_*(info, ...)
   │   1. Selection.from_info(info) → derive want_* flags + per-level Selection
   │      objects for each nested batch
   │   2. _title_cols(driver, sel)   → project ONLY the requested SQL columns
   │   3. SQL on main + titledb (count is gated on `total`, page is gated on `items`)
   │   4. Hydration chain (see below) — only the requested branches run
   │
   ▼
Strawberry types → JSON response (response gets the ETag header)
```

## Selection-aware hydration

`selection.Selection` is a small wrapper around `info.selected_fields`. Two
methods, both intentionally trivial:

- `has(name)` — is this field selected at the current level?
- `child(name)` — descend one level into the named field's sub-selection.

Resolvers use it in two places:

1. **Skip unrequested batch queries.** If the client didn't ask for `apps` on
   `Title`, `_load_apps_for_titles` doesn't run. Same for `App.files`,
   `App.titledb`, `File.apps`, `Connection.total`, `Connection.items`. A query
   asking only for `{ titles { total } }` runs one COUNT(*) and nothing else.
2. **Project only requested columns.** `_TITLE_COL_MAP` maps each Title field's
   GraphQL camelCase name to its SQL fragment. `_title_cols(driver, sel)` emits
   only the columns the client selected, plus the always-needed `title_id` and
   `source`. A `{titleId, name, bannerUrl, iconUrl}` query SELECTs ~4 columns
   instead of all ~30 — meaningful because some of the unselected ones
   (`description`, `intro`, `screenshots`, `ratingContent`) are large
   text/JSON blobs.

Field names in selection paths are **GraphQL camelCase** (`availableVersions`,
`bannerUrl`, `releaseDate`) — not Python snake_case.

`_build_title` reads row columns via `row._mapping.get(...)`, so unselected
columns map cleanly to `None` instead of raising `AttributeError`.

## Hydration chain

Every nested batch-loaded field follows the same two-piece pattern:

- A `Private[Optional[List[T]]]` slot on the parent dataclass (`apps_loaded`,
  `files_loaded`, `titledb_loaded`). The parent resolver populates it eagerly
  before returning.
- A `@strawberry.field` resolver method that returns the list, applies any
  in-memory filter (`match_app` / `match_file`), and returns `None` if the slot
  is `None`. `None` means "not loaded" (admin-gated or selection-skipped); `[]`
  means "loaded, empty".

The deepest path the chain handles (Title → apps → files → back-link apps):

```
resolve_titles / resolve_title
 └─ _load_apps_for_titles            (one batched SELECT for apps in this page)
      ├─ _hydrate_app_files          (one batched SELECT joining app_files → files)
      │    └─ _hydrate_file_apps     (one batched SELECT for back-link apps)
      ├─ _hydrate_apps_titledb       (titledb metadata for the top-level apps)
      └─ _hydrate_app_versions       (version history, two batched SELECTs)

resolve_apps
 ├─ _hydrate_app_files
 ├─ _hydrate_apps_titledb
 ├─ _hydrate_apps_title              (the parent title of each app)
 └─ _hydrate_app_versions

resolve_files
 └─ _hydrate_file_apps               (apps owning each file in this page)
      ├─ _hydrate_apps_titledb       (titledb metadata for those apps)
      ├─ _hydrate_apps_title         (names the game behind an UPDATE file)
      └─ _hydrate_app_versions
```

`App.titledb`, `App.title` and `App.versions` are hydrated only one hop below a
top-level resolver. Apps reached as the back-link of an app's own files
(`apps { files { apps } }`) carry none of them, so `_hydrate_file_apps` takes
those arguments only from `resolve_files`.

`App.title` is not hydrated under `Title.apps` — the caller already *is* the
title, and re-fetching it per app would be a round trip for data the client
already has in hand.

Each helper takes:

- `with_*` flags — the gating decisions, derived from the caller's `Selection`.
- `*_sel: Selection` — the selection at the next level down, used to project
  columns dynamically when that helper itself runs SQL on titles.

The back-link path stops recursing on `files_loaded=None` for back-link apps,
so a query like `apps { files { apps { files { apps ... }}}}` collapses after
the first pass — there's no infinite loop.

## Schema features that affect performance

- **`titledb.titles` is pre-merged** — one row per title id, with the metadata
  sources already resolved field by field at import time (and per-id on every
  override write, in `titledb.store.set_override` / `delete_override`). The
  resolvers' titledb join therefore carries no dedup predicate at all: no
  correlated `NOT EXISTS`, no `source`/`is_overridden` filter, no duplicate ids
  to collapse in a paged query.
- **`apps.release_date`**. UPDATE rows carry their titledb release date
  directly; BASE/DLC rows leave it `NULL`. Populated by
  `add_missing_apps_for_title` with `on_conflict_do_update` set_=release_date
  only — never touch `owned`, which a concurrent file scan may have flipped.
  Lets the UI build the version grid from
  `Title.apps.filter(appType === 'UPDATE')` instead of a separate
  `availableVersions` query against `titledb.versions`.
- **`ix_app_files_file_id`**. The composite PK `(app_id, file_id)` only indexes
  left-to-right; back-link queries (`WHERE file_id IN (...)`) need this
  explicit index. SQLite does not auto-index foreign keys (PostgreSQL does);
  SQLAlchemy `relationship()` is purely Python-side and creates no index.
  Critical for `_hydrate_file_apps`, called by both `resolve_files` and any
  `Title.apps.files.apps` selection.

## Filtering

Implicit AND across populated fields. v1 has no OR / NOT combinators.

- **Top-level**: `build_clauses` translates the filter input into parameterized
  SQL fragments (safe — values are bound, columns are server-side strings).
- **Nested**: filters on `Title.apps(filter: ...)` etc. are applied in-memory
  against the already-hydrated list using `match_app` / `match_file`. Same
  semantics as the SQL clauses.
- **Booleans are bare**: `filter: {multicontent: true}`, not `{multicontent:
  {eq: true}}`. Equality is the only predicate a bool has, so an operator object
  would carry no information. Strings and ints keep theirs, which makes a filter
  input deliberately mixed-shape.
- **Shorthand args**: `owned` and `appType` are first-class arguments on the
  `apps` query and on the `Title.apps` field, AND-ed with any `filter` covering
  the same column. `appType` is a list, so GraphQL coerces both `appType: "DLC"`
  and `appType: ["DLC", "UPDATE"]`; an empty list is no constraint, matching
  `StringFilter.in`. `File.apps` takes `appType` but **not** `owned`: an app is
  owned exactly when it has files, so a file's apps are all owned and the
  argument could only ever return everything or nothing.
- **One meaning per predicate**: `owned:` and `filter: {owned:}` are the same
  clause, emitted once in `resolve_apps` rather than by two code paths. Under
  `groupByAppId: true` that clause is `HAVING MAX(a.owned)` — owned is a
  property of the app id as a whole, "any version of it" — for both spellings;
  ungrouped it is the row's own column. `APP_FIELDS_EXCEPT_OWNED` is what keeps
  `build_clauses` from also emitting the row-level form and splitting the two
  spellings apart again.
- **Ownership is false, not unknown, for a title the library has never seen**.
  `haveBase` / `upToDate` / `complete` live on `main.titles`, which is LEFT
  JOINed, so a catalogue-only title has no row there. The `TITLE_FIELDS`
  expressions `COALESCE` the NULL to 0: without it `NULL = 0` is NULL and both
  polarities matched nothing, which silently dropped every catalogue title from
  an ownership-filtered query. Note this is deliberately *not* what
  `Title.ownership` does — that stays null, meaning "no library row".

JSON-list columns on `Title` (`category`, `regions`, `languages`,
`screenshots`, `ratingContent`, `ids`) are stored as JSON-encoded strings in
titledb. The current `StringFilter.contains` matches the JSON encoding;
`StringFilter.eq` is unusable on these. Don't filter them unless that's
understood.

## Ordering

`orderBy: {field: SIZE, direction: DESC}` on `titles`, `apps` and `files`.

- **The client picks from an enum, never a column name.** `OrderField` →
  SQL expression is a server-side whitelist per query (`TITLE_ORDER`,
  `APP_ORDER`, `FILE_ORDER` in `filters.py`), so nothing a caller sends is
  interpolated into `ORDER BY`.
- **A field a query has no column for degrades to that query's default order**
  rather than erroring — `DOWNLOAD_COUNT` on `titles` is meaningless, not
  invalid.
- **Every ordering appends the query's default as a tie-break.** Sorting on a
  non-unique column without one lets equal rows swap between pages, so a client
  paging through sees one row twice and another never.
- **NULL-placement flags don't take the direction.** `td.name IS NULL` stays
  unsuffixed so `DESC` doesn't float unrecognized titles to the top.

`files.added_at` exists for this: `mtime` is the filesystem's modification time,
which a re-organize or a copy rewrites, so it cannot answer "recently added".

## ETag / cache

`view.graphql_dispatch` sets `ETag` on every response and short-circuits with
`304` when `If-None-Match` matches:

- ETag = MD5 of `(query, variables, operationName, role, world_hash)`.
- `world_hash` = MD5 of one aggregate row per source table, plus
  `titledb.meta.imported_at`:
  - `main.titles` — count, `SUM(have_base)`, `SUM(up_to_date)`, `SUM(complete)`
  - `apps` — count, `SUM(owned)`, `MAX(id)`
  - `files` — count, `MAX(id)`, `SUM(organized)`, `SUM(identified)`,
    `SUM(identification_attempts)`, `SUM(download_count)`
  - `tasks` — count, `MAX(id)`, `SUM(completion_pct)`
- `role` is `admin` / `shop` / `anon` — different roles see different
  field-level gates so they can't share cache entries.
- Headers: `Cache-Control: private, must-revalidate; Vary: Authorization,
  Cookie`.

GET works with browser auto-revalidation. POST clients must capture and re-send
the ETag manually.

**Why aggregates rather than a revision counter.** A counter bumped by each
writer is cheaper per request, but it couples every write path to the cache and
fails silently when someone forgets. SQLite stores no row count, so `COUNT(*)`
already walks the whole B-tree — folding the mutable columns into that same scan
is close to free and makes invalidation a property rather than a convention.
This is what catches `update_titles` flipping `have_base` / `up_to_date` /
`complete`, the `organized` flag, identification status, and `download_count`,
none of which change any row count.

**The rule when adding a field.** If you expose a column the schema could not
previously see, add it to `_WORLD_SQL` in `cache.py` — otherwise a change to it
serves a stale `304`. Immutable columns and anything already covered by a count
need nothing.

**Cost.** Running tasks update `completion_pct` continuously, so an active scan
invalidates the cache on every progress write. That is the intended trade: while
a scan runs the library really is changing, and the `files` count is moving too.

## Mutations

`mutations.py` holds the write root. Scope is library-domain only — settings, users
and the keys upload stay on REST, being form-and-file shaped rather than graph
shaped. Every resolver delegates to existing code; no business logic lives there.

| Mutation | Delegates to |
|---|---|
| `enqueueTask(name, input)` | `tasks.enqueue_task` |
| `cancelTask(id)` | `tasks.cancel_task` |
| `scanLibrary(path)` | `enqueue_task('scan_library')`, all libraries when `path` is omitted |
| `compressFile(fileId)` / `decompressFile(fileId)` | `enqueue_task`, guarded on file exists / not already in that state / extension in `COMPRESS_EXT` |
| `setTitleOverride(titleId, record)` | `titledb.store.set_override` + `identify_library` |
| `deleteTitleOverride(titleId)` | `titledb.store.delete_override` |

Three conventions differ from the query side, deliberately:

- **Denial raises.** Queries return `None` for a field a role cannot read, which is
  the right shape for a partial result. A write that is silently ignored is not a
  partial result, so `NotAuthorized` / `MutationFailed` surface as GraphQL errors.
- **Nothing is cached.** `view.is_mutation` parses the document — it does not match
  on the string, so a query named `mutationStatus` is not caught — and mutations skip
  both the `If-None-Match` short-circuit and the `ETag` header, answering `no-store`.
- **GET is refused with 405.** A GET URL is cacheable, prefetchable and
  link-followable; it must not have side effects.

Mutations that change counts or flags are picked up by the world hash automatically
(see the ETag section), so a write invalidates the read side with no extra wiring.
`setTitleOverride` / `deleteTitleOverride` are covered a different way: `_project_override`
bumps `titledb.meta.imported_at`, which the world hash reads.

**Known edge.** `imported_at` has one-second resolution, so two metadata edits landing
within the same second produce the same world hash and the second can be served a stale
`304`. Only reachable by a client writing overrides faster than a human form can — a UI
that saves a metadata form field by field would want to be aware of it.

`JSON` payloads (`enqueueTask.input`, `setTitleOverride.record`) are passed as
strings: their shape differs per task name / per metadata source, and the schema
cannot describe a union of every registered task's input.

## Replaced REST routes

These lived in `app/app.py` and were removed once the mutation root landed. Listed so
that a search for the old path finds where the capability went:

| Removed route | Now |
|---|---|
| `POST /api/library/scan` | `scanLibrary` |
| `POST /api/tasks` | `enqueueTask` |
| `GET /api/tasks` | `tasks` query |
| `GET /api/tasks/<id>` | `task` query |
| `DELETE /api/tasks/<id>` | `cancelTask` |
| `POST /api/files/<id>/compress` \| `/decompress` | `compressFile` / `decompressFile` |
| `POST /api/titledb/custom` | `setTitleOverride` |
| `DELETE /api/titledb/custom/<id>` | `deleteTitleOverride` |
| `GET /api/titledb/custom` | **nothing** — listing overrides has no GraphQL equivalent |
| `DELETE /api/tasks/failed` | **nothing** — purging failed tasks has no GraphQL equivalent |

The last two were dropped rather than ported: neither had a caller. Recoverable from
git if a use turns up.

`GET /api/get_game/<id>` stays REST — it streams a file to shop clients
(`shop.py`, `clients/tinfoil.py`, `clients/cyberfoil.py`), and `File.url` is deferred
with the console-client work.

## Query depth

`QueryDepthLimiter(max_depth=MAX_QUERY_DEPTH)` — 15. The deepest query the UI issues
is about 7 levels, so this leaves headroom while refusing the pathological nestings
the hydration chain would otherwise expand, on an endpoint any shop-access user can
reach.

## Auth

Two-tier:

- **View-level**: `view.graphql_dispatch` returns 401 if not authenticated, 403
  if neither `has_shop_access()` nor `has_admin_access()`.
- **Field-level**: resolvers return `None` for fields the role isn't entitled
  to. `apps`/`titles`/`title` require `can_shop`; `files` and `App.files`
  require `can_admin`; `File.filepath` is null for non-admin. Schema
  introspection still shows these fields exist (Strawberry doesn't conditionally
  hide), but the data isn't exposed.

## Adding a new field

For a column on an existing type:

1. Add the column to the model in `app/db.py` and create an Alembic migration.
2. Add it to the Strawberry type in `gql/types.py`.
3. Project the column in the SQL — for `Title` fields, add an entry to
   `_TITLE_COL_MAP`; for `App` / `File`, edit the `_FILE_COLS` constant or the
   resolver SQL directly.
4. Read it via `m.get(col)` in `_build_title` / `_build_file` so unselected
   columns don't crash.

For a brand-new nested batch-loaded field:

1. Add `Private[Optional[List[T]]] = None` slot on the parent type, plus a
   `@strawberry.field` resolver method that returns it (or filters in-memory).
2. Add a `_hydrate_xxx(parent_pks, parents_by_pk, *, ..., sel: Selection)`
   helper that runs ONE batched SELECT and attaches results.
3. In the parent resolver, derive `want_xxx = ctx.<gate> and parent_sel.has("xxxName")`
   and `xxx_sel = parent_sel.child("xxxName")`, then call the hydrator.
4. If your new field needs sub-selection projection on titledb data, pass a
   `titledb_sel` Selection to `_hydrate_apps_titledb` from the top-level
   resolver — don't thread it down through the nested hydrators.

## Adding a new top-level query

1. Add the field on `Query` in `gql/schema.py` (always take `info: Info` so the
   resolver can do selection inspection).
2. Implement `resolve_xxx(*, ..., ctx, info)` in `gql/resolvers.py`.
3. Build a `Connection`-style return (`{ total, items }`) and gate both `total`
   and `items` on `Selection.has(...)`.
