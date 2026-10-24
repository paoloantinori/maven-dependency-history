"""Run-local bounded raw byte sink after durable paired consumption.

The sink reloads the validated full chain under the run writer lock. Both
operations must still be consumed in their actual shared transition revision;
a descriptor or matching timestamps alone grant nothing. The output is created
once, never resumed, appended to, printed or uploaded. A crash may leave a
bounded private partial file; its existence prevents reopening with old consent.

Keep this sink open only during its associated source execution. Its writer
lock prevents cooperating report publishers from finalizing or starting another
capture until it closes. Reads remain possible. This is a trusted coordinator
contract, not a sandbox against arbitrary same-user writes or fabricated report
history. The sink records a digest of stored bytes; it does not assert that a
caller supplied every byte of the source stream.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

from mdi import runstore
from mdi.report import _json_equal

TOOL_IDENTITY = {
    'name': 'mdi.raw_capture_store', 'version': '1',
    'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
}


class RawCaptureError(ValueError):
    """Refusal or failed capture, with bounded cleanup/capture metadata."""

    def __init__(self, message, capture=None):
        super().__init__(message)
        self.capture = capture


class RawCaptureSink:
    def __init__(self, directory_fd, output_fd, held, path, name, cap):
        self.directory_fd = directory_fd
        self.output_fd = output_fd
        self.held = held
        self.path = path
        self.name = name
        self.cap = cap
        initial = os.fstat(output_fd)
        self.identity = runstore._fd_identity(initial)
        self.observed = (initial.st_mtime_ns, initial.st_ctime_ns)
        self.digest = hashlib.sha256()
        self.size = 0
        self.truncated = False
        self.failed = False
        self.result = None

    @classmethod
    def open(cls, store, capture_operation_id, *, expected_revision):
        """Create the exact approved basename once; never consumes approval itself."""
        directory_fd = output_fd = lock_fd = None
        held = None
        path = None
        created = False
        try:
            runstore._require_primitives()
            directory_fd = runstore._open_dir_nofollow(runstore._lexical_abs(store.path))
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != runstore.RUN_DIR_MODE:
                raise RawCaptureError('raw capture requires a private owned run directory')
            lock_fd, held = runstore._acquire_writer_lock(directory_fd)
            bindings = {}
            report = runstore._read_chain(directory_fd, store.run_id, raw_bindings=bindings)
            if type(expected_revision) is not int or report['revision'] != expected_revision:
                raise RawCaptureError('raw capture revision is stale')
            matches = [value for value in bindings.values()
                       if value is not None and value[0]['id'] == capture_operation_id]
            if len(matches) != 1:
                raise RawCaptureError('raw capture requires registered paired consumption')
            capture, source, consumed_revision = matches[0]
            if capture['state'] != 'consumed' or source['state'] != 'consumed':
                raise RawCaptureError('source and capture must still be consumed')
            if consumed_revision != expected_revision:
                raise RawCaptureError('capture must open at the paired consumption revision')
            if not _json_equal(capture['tool_identity'], TOOL_IDENTITY):
                raise RawCaptureError('capture tool identity differs from this sink')
            scope = capture['scope']
            name = scope['output_name']
            path = os.path.join(store.path, name)
            output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
            created = True
            os.fchmod(output_fd, 0o600)
            sink = cls(directory_fd, output_fd, held, path, name, scope['max_bytes'])
            os.fsync(directory_fd)
            # The lock's inode/nonce remains held; no lock descriptor is needed.
            fd, lock_fd = lock_fd, None
            os.close(fd)
            directory_fd = output_fd = None
            held = None
            return sink
        except BaseException as exc:
            # Even an empty newly created file spends the one-create lifetime.
            metadata = {'path': path if created else None, 'status': 'unconfirmed', 'retained': created}
            for fd in (output_fd, lock_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except BaseException:
                        exc.add_note('raw capture descriptor closure unconfirmed')
            if directory_fd is not None:
                if held is not None:
                    try:
                        problem = runstore._release_writer_lock(directory_fd, held)
                        if problem:
                            exc.add_note('raw capture lock release unconfirmed')
                    except BaseException:
                        exc.add_note('raw capture lock release unconfirmed')
                try:
                    os.close(directory_fd)
                except BaseException:
                    exc.add_note('raw capture directory closure unconfirmed')
            if isinstance(exc, RawCaptureError):
                exc.capture = metadata
                raise
            if isinstance(exc, Exception):
                raise RawCaptureError('raw capture could not be opened', metadata) from exc
            exc.raw_capture = metadata
            raise

    def _check_identity(self):
        if self.output_fd is None or self.directory_fd is None:
            raise RawCaptureError('raw capture is closed', self.result)
        held = os.fstat(self.output_fd)
        named = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        for info in (held, named):
            if (runstore._fd_identity(info) != self.identity or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != self.size
                    or (info.st_mtime_ns, info.st_ctime_ns) != self.observed):
                raise RawCaptureError('raw capture file identity changed')

    def write(self, data):
        """Store only the prefix within the approved cap; report retained byte count."""
        if not isinstance(data, bytes):
            raise TypeError('raw capture accepts bytes only')
        try:
            self._check_identity()
            count = min(len(data), self.cap - self.size)
            self.truncated = self.truncated or len(data) > count
            view = memoryview(data)
            offset = 0
            while offset < count:
                written = os.write(self.output_fd, view[offset:min(count, offset + 65536)])
                if written <= 0:
                    raise OSError('short raw capture write')
                self.digest.update(view[offset:offset + written])
                self.size += written
                offset += written
            info = os.fstat(self.output_fd)
            self.observed = (info.st_mtime_ns, info.st_ctime_ns)
            self._check_identity()
            return count
        except BaseException:
            self.failed = True
            raise

    def close(self, *, interrupted=False):
        """Retain bounded bytes, release owned handles once, return metadata only."""
        if self.result is not None:
            return self.result
        primary = sys.exception()
        error = None
        verified = False
        try:
            self._check_identity()
            os.fsync(self.output_fd)
            verified = True
        except BaseException as exc:
            self.failed = True
            error = exc
        fd, self.output_fd = self.output_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                error = error or exc
        if self.directory_fd is not None:
            try:
                if runstore._release_writer_lock(self.directory_fd, self.held) is not None:
                    error = error or RawCaptureError('raw capture lock release unconfirmed')
            except BaseException as exc:
                error = error or exc
            fd, self.directory_fd = self.directory_fd, None
            try:
                os.close(fd)
            except BaseException as exc:
                error = error or exc
        self.held = None
        partial = interrupted or self.failed or self.truncated or error is not None
        self.result = {'path': self.path, 'bytes': self.size,
                       'status': 'partial' if partial else 'complete', 'retained': True,
                       'truncated': self.truncated,
                       'stored_sha256': self.digest.hexdigest() if verified else None,
                       'stream_sha256': None,
                       'durability': 'unconfirmed' if error is not None else 'file-fsynced',
                       'limitations': ['stored-byte digest does not prove complete source-stream coverage',
                                       'raw bytes may contain credentials; no sanitization or upload']}
        if error is not None:
            if primary is not None:
                primary.add_note('raw capture finalization unconfirmed')
            elif isinstance(error, Exception):
                raise RawCaptureError('raw capture finalization unconfirmed', self.result) from error
            else:
                error.raw_capture = self.result
                raise error
        return self.result

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close(interrupted=kind is not None)
        if value is not None:
            value.raw_capture = self.result
        return False
