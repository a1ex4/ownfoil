"""Mutation root: library-domain writes.

Scope is deliberately narrow. Settings, users and the keys upload stay on REST -
they are form-and-file shaped, not graph shaped. What lives here is everything a
library page needs to act on what it is displaying: enqueue work, cancel work, and
edit title metadata.

Two conventions differ from the query side, both on purpose:

- **Denial raises.** Queries return `None` for a field a role cannot read, which is
  the right shape for a partial result. A write that is silently ignored is not a
  partial result, it is a lie, so these raise instead.
- **Nothing is cached.** `view.graphql_dispatch` skips the ETag and the 304 path
  entirely for mutations - see `is_mutation` there.

Every resolver delegates; no business logic lives in this module.
"""
from typing import List, Optional

import strawberry
from strawberry.types import Info
from typing_extensions import Annotated

from constants import COMPRESS_EXT

from .docs import desc, described, described_mutation
from .resolvers import resolve_task, resolve_title
from .types import Task, Title


class NotAuthorized(Exception):
    """Raised when a role may not perform a write. Surfaces as a GraphQL error."""


class MutationFailed(Exception):
    """A write that was refused on its merits (unknown task, wrong file state)."""


def _require_admin(ctx) -> None:
    if not ctx.can_admin:
        raise NotAuthorized("Admin access is required for this operation.")


def _task_by_id(task_id, info) -> Optional[Task]:
    """Re-read a task through the query resolver so a mutation returns exactly what
    `task(id:)` would - one shape for a task, however the client got there."""
    return resolve_task(str(task_id), info.context, info)


@described(strawberry.input)
class FileRefInput:
    """A file as the client displayed it. A deletion acts on the file only while both
    still match, so a stale view never deletes a file it did not show."""
    id: strawberry.ID = desc("Primary key of the file row.")
    filepath: str = desc("Absolute path of the file when it was displayed.")


def _enqueue_removal(task_name, files, info) -> Optional[Task]:
    import tasks as tasks_mod
    _require_admin(info.context)
    if not files:
        raise MutationFailed("No files given")
    task, _ = tasks_mod.enqueue_task(task_name, {
        'files': [{'id': int(f.id), 'filepath': f.filepath} for f in files]})
    return _task_by_id(task.id, info)


@described(strawberry.type)
class Mutation:
    """Library-domain writes: enqueue work, cancel work, edit title metadata.

    Deliberately narrow - settings, users and the keys upload stay on REST, being
    form-and-file shaped rather than graph shaped. Unlike the query side, a write a
    role may not perform raises rather than returning null: a silently ignored write
    is not a partial result, it is a lie. All of these require admin."""

    @described_mutation
    def enqueue_task(
        self, info: Info,
        name: Annotated[str, strawberry.argument(
            description="A registered task name, e.g. `process_library`. An "
                        "unknown name is refused.")],
        input: Annotated[Optional[str], strawberry.argument(
            description="The task's arguments as a JSON object string. Omit for a "
                        "task that takes none.")] = None,
    ) -> Optional[Task]:
        """Enqueue any registered task. `input` is a JSON object string, because the
        payload shape differs per task name. Enqueuing a duplicate returns the
        existing task rather than creating a second one."""
        import json
        import tasks as tasks_mod
        _require_admin(info.context)
        try:
            payload = json.loads(input) if input else {}
        except ValueError as e:
            raise MutationFailed(f"input is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise MutationFailed("input must be a JSON object")
        try:
            task, _created = tasks_mod.enqueue_task(name, payload)
        except ValueError as e:
            raise MutationFailed(str(e))
        return _task_by_id(task.id, info)

    @described_mutation
    def cancel_task(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the task to cancel.")],
    ) -> bool:
        """False when the task is unknown or already in a terminal state."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return bool(tasks_mod.cancel_task(int(id)))

    @described_mutation
    def dismiss_task(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the failed task to remove.")],
    ) -> bool:
        """Clear one failed task. Failed tasks are kept so a failure survives a page
        reload, so this is how a task queue gets tidied once its failures have been
        read. False when the task is unknown or has not failed - a running or queued
        task is `cancelTask`'s job, not this one."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return bool(tasks_mod.dismiss_task(int(id)))

    @described_mutation
    def purge_failed_tasks(self, info: Info) -> int:
        """Clear every failed task at once, returning how many were removed. Zero when
        there was nothing to clear, which is not an error."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return tasks_mod.purge_failed_tasks()

    @described_mutation
    def scan_library(
        self, info: Info,
        path: Annotated[Optional[str], strawberry.argument(
            description="Absolute path of one configured library root. Omit to scan "
                        "every configured root.")] = None,
    ) -> Optional[Task]:
        """Scan one library, or every configured library when `path` is omitted. The
        all-libraries form returns the last task enqueued."""
        import tasks as tasks_mod
        from db import get_libraries
        _require_admin(info.context)
        if path:
            task, _ = tasks_mod.enqueue_task('scan_library', {'library_path': path})
            return _task_by_id(task.id, info)
        last = None
        for lib in get_libraries():
            last, _ = tasks_mod.enqueue_task('scan_library', {'library_path': lib.path})
        return _task_by_id(last.id, info) if last else None

    @described_mutation
    def compress_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to compress.")],
    ) -> Optional[Task]:
        """Compress one file to NSZ/XCZ, whether or not automatic compression is on."""
        import tasks as tasks_mod
        from db import Files, db
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if file.compressed:
            raise MutationFailed("File is already compressed")
        if file.extension not in COMPRESS_EXT:
            raise MutationFailed("File type cannot be compressed")
        task, _ = tasks_mod.enqueue_task('compress_file', {'file_id': int(file_id), 'manual': True})
        return _task_by_id(task.id, info)

    @described_mutation
    def decompress_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to decompress.")],
    ) -> Optional[Task]:
        """Decompress one file back to NSP/XCI."""
        import tasks as tasks_mod
        from db import Files, db
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if not file.compressed:
            raise MutationFailed("File is not compressed")
        task, _ = tasks_mod.enqueue_task('decompress_file', {'file_id': int(file_id)})
        return _task_by_id(task.id, info)

    @described_mutation
    def verify_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to verify.")],
    ) -> Optional[Task]:
        """Re-verify one file at the configured depth, whether or not automatic
        verification is on. The stored verdicts are cleared first, so this re-checks a
        file that already has them rather than no-opping. Refused without console keys,
        which verification cannot run without."""
        import tasks as tasks_mod
        from containers import verification as verification_lib
        from db import Files, db, reset_file_verification
        from settings import load_keys
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if file.extension not in verification_lib.VERIFY_EXT:
            raise MutationFailed("File type cannot be verified")
        if not load_keys()[0]:
            raise MutationFailed("Verification needs console keys")
        reset_file_verification(file)
        db.session.commit()
        task, _ = tasks_mod.enqueue_task('verify_file', {'file_id': int(file_id), 'manual': True})
        return _task_by_id(task.id, info)

    @described_mutation
    def retry_identification(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to identify again.")],
    ) -> Optional[Task]:
        """Identify one file again. A file that failed identification is otherwise never
        retried; this clears its attempts and sends it back down the pipeline."""
        import tasks as tasks_mod
        from db import Files, db, reset_file_identification
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        reset_file_identification(file)
        db.session.commit()
        task, _ = tasks_mod.enqueue_task('process_file', {'file_id': int(file_id)})
        return _task_by_id(task.id, info)

    @described_mutation
    def delete_file(
        self, info: Info,
        file: Annotated[FileRefInput, strawberry.argument(
            description="The file to delete, as it was displayed.")],
    ) -> Optional[Task]:
        """Delete one file from disk and the library. Runs as a task, one deletion at a
        time; the task fails if the file is being processed, and does nothing if the
        file moved since it was displayed."""
        import tasks as tasks_mod
        from db import Files, db
        _require_admin(info.context)
        row = db.session.get(Files, int(file.id))
        if not row or row.filepath != file.filepath:
            raise MutationFailed("File changed since it was displayed")
        task, _ = tasks_mod.enqueue_task('delete_file', {'file_id': row.id,
                                                         'filepath': row.filepath})
        return _task_by_id(task.id, info)

    @described_mutation
    def remove_duplicates(
        self, info: Info,
        files: Annotated[List[FileRefInput], strawberry.argument(
            description="The duplicates to delete, as `duplicates` listed them.")],
    ) -> Optional[Task]:
        """Delete previewed duplicates, whether or not automatic deduplication is on.
        The set is recomputed when the task runs and only files in both are deleted, so
        nothing is deleted that was not previewed or is no longer a duplicate."""
        return _enqueue_removal('remove_duplicates', files, info)

    @described_mutation
    def remove_outdated_updates(
        self, info: Info,
        files: Annotated[List[FileRefInput], strawberry.argument(
            description="The update files to delete, as `outdatedUpdates` listed them.")],
    ) -> Optional[Task]:
        """Delete previewed outdated update files, whether or not the setting is on. As
        with `removeDuplicates`, only files still outdated when the task runs go."""
        return _enqueue_removal('remove_outdated_updates', files, info)

    @described_mutation
    def set_title_override(
        self, info: Info,
        title_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit title id to override.")],
        record: Annotated[str, strawberry.argument(
            description="A JSON object of metadata fields to override. Fields it "
                        "omits keep their downloaded values.")],
    ) -> Optional[Title]:
        """Write user-authored metadata for a title, winning over the downloaded
        titledb values field by field. `record` is a JSON object of the same shape the
        REST endpoint takes. Re-identification is enqueued, as there too."""
        import json
        import tasks as tasks_mod
        import titledb
        _require_admin(info.context)
        try:
            payload = json.loads(record)
        except ValueError as e:
            raise MutationFailed(f"record is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise MutationFailed("record must be a JSON object")
        ok, err = titledb.store.set_override(str(title_id), payload)
        if not ok:
            raise MutationFailed(err)
        tasks_mod.enqueue_task('process_library')
        return resolve_title(str(title_id), info.context, info)

    @described_mutation
    def delete_title_override(
        self, info: Info,
        title_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit title id whose override to drop.")],
    ) -> bool:
        """Drop the override, restoring the next metadata source down."""
        import titledb
        _require_admin(info.context)
        ok, _err = titledb.store.delete_override(str(title_id))
        return bool(ok)
