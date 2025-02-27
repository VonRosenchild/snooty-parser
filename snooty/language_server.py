import logging
import os
import sys
import threading
import urllib.parse
import pyls_jsonrpc.dispatchers
import pyls_jsonrpc.endpoint
import pyls_jsonrpc.streams
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePath
from typing import cast, Any, BinaryIO, Callable, Dict, List, Optional, Union, TypeVar
from .flutter import checked, check_type
from .types import FileId, SerializableType
from . import types, util
from .parser import Project

_F = TypeVar("_F", bound=Callable[..., Any])
Uri = str
PARENT_PROCESS_WATCH_INTERVAL_SECONDS = 60
logger = logging.getLogger(__name__)


class debounce:
    def __init__(self, wait: float) -> None:
        self.wait = wait

    def __call__(self, fn: _F) -> _F:
        wait = self.wait

        @wraps(fn)
        def debounced(*args: Any, **kwargs: Any) -> Any:
            def action() -> None:
                fn(*args, **kwargs)

            try:
                getattr(debounced, "debounce_timer").cancel()
            except AttributeError:
                pass
            timer = threading.Timer(wait, action)
            setattr(debounced, "debounce_timer", timer)
            timer.start()

        return cast(_F, debounced)


@checked
@dataclass
class Position:
    line: int
    character: int


@checked
@dataclass
class Range:
    start: Position
    end: Position


@checked
@dataclass
class Location:
    uri: Uri
    range: Range


@checked
@dataclass
class TextDocumentIdentifier:
    uri: Uri


@checked
@dataclass
class TextDocumentItem:
    uri: Uri
    languageId: str
    version: int
    text: str


@checked
@dataclass
class VersionedTextDocumentIdentifier(TextDocumentIdentifier):
    version: Union[int, None]


@checked
@dataclass
class TextDocumentContentChangeEvent:
    range: Optional[Range]
    rangeLength: Optional[int]
    text: str


@checked
@dataclass
class DiagnosticRelatedInformation:
    location: Location
    message: str


@checked
@dataclass
class Diagnostic:
    range: Range
    severity: Optional[int]
    code: Union[int, str, None]
    source: Optional[str]
    message: str
    relatedInformation: Optional[List[DiagnosticRelatedInformation]]


@checked
@dataclass
class Command:
    title: str
    command: str
    arguments: Optional[object]


@checked
@dataclass
class TextEdit:
    range: Range
    newText: str


@checked
@dataclass
class TextDocumentEdit:
    textDocument: VersionedTextDocumentIdentifier
    edits: List[TextEdit]


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


class Backend:
    def __init__(self, server: "LanguageServer") -> None:
        self.server = server

    def on_progress(self, progress: int, total: int, message: str) -> None:
        pass

    def on_diagnostics(self, path: FileId, diagnostics: List[types.Diagnostic]) -> None:
        self.server.set_diagnostics(path, diagnostics)

    def on_update(self, prefix: List[str], page_id: FileId, page: types.Page) -> None:
        pass

    def on_delete(self, page_id: FileId) -> None:
        pass


@dataclass
class WorkspaceEntry:
    page_id: FileId
    document_uri: Uri
    diagnostics: List[types.Diagnostic]

    def create_lsp_diagnostics(self) -> List[object]:
        return [
            {
                "range": {
                    "start": {
                        "line": diagnostic.start[0],
                        "character": diagnostic.start[1],
                    },
                    "end": {"line": diagnostic.end[0], "character": diagnostic.end[1]},
                },
                "severity": diagnostic.severity,
                "message": diagnostic.message,
            }
            for diagnostic in self.diagnostics
        ]


class LanguageServer(pyls_jsonrpc.dispatchers.MethodDispatcher):
    def __init__(self, rx: BinaryIO, tx: BinaryIO) -> None:
        self.project: Optional[Project] = None
        self.workspace: Dict[str, WorkspaceEntry] = {}
        self.diagnostics: Dict[PurePath, List[types.Diagnostic]] = {}

        self._jsonrpc_stream_reader = pyls_jsonrpc.streams.JsonRpcStreamReader(rx)
        self._jsonrpc_stream_writer = pyls_jsonrpc.streams.JsonRpcStreamWriter(tx)
        self._endpoint = pyls_jsonrpc.endpoint.Endpoint(
            self, self._jsonrpc_stream_writer.write
        )
        self._shutdown = False

    def start(self) -> None:
        self._jsonrpc_stream_reader.listen(self._endpoint.consume)

    def set_diagnostics(
        self, fileid: FileId, diagnostics: List[types.Diagnostic]
    ) -> None:
        self.diagnostics[fileid] = diagnostics
        uri = self.fileid_to_uri(fileid)
        workspace_item = self.workspace.get(uri, None)
        if workspace_item is None:
            workspace_item = WorkspaceEntry(fileid, uri, [])

        workspace_item.diagnostics = diagnostics
        self._endpoint.notify(
            "textDocument/publishDiagnostics",
            params={"uri": uri, "diagnostics": workspace_item.create_lsp_diagnostics()},
        )

    def uri_to_fileid(self, uri: Uri) -> FileId:
        if not self.project:
            raise TypeError("Cannot map uri to fileid before a project is open")

        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError("Only file:// URIs may be resolved", uri)

        path = Path(parsed.netloc).joinpath(Path(parsed.path)).resolve()
        return self.project.get_fileid(path)

    def fileid_to_uri(self, fileid: FileId) -> str:
        if not self.project:
            raise TypeError("Cannot map fileid to uri before a project is open")

        return "file://" + str(self.project.config.source_path.joinpath(fileid))

    def m_initialize(
        self,
        processId: Optional[int] = None,
        rootUri: Optional[Uri] = None,
        **kwargs: object,
    ) -> SerializableType:
        if rootUri:
            root_path = Path(rootUri.replace("file://", "", 1))
            self.project = Project(root_path, Backend(self))
            self.project.build()

        if processId is not None:

            def watch_parent_process(pid: int) -> None:
                # exist when the given pid is not alive
                if not pid_exists(pid):
                    logger.info("parent process %s is not alive", pid)
                    self.m_exit()
                logger.debug("parent process %s is still alive", pid)
                threading.Timer(
                    PARENT_PROCESS_WATCH_INTERVAL_SECONDS,
                    watch_parent_process,
                    args=[pid],
                ).start()

            watching_thread = threading.Thread(
                target=watch_parent_process, args=(processId,)
            )
            watching_thread.daemon = True
            watching_thread.start()

        return {"capabilities": {"textDocumentSync": 1}}

    def m_initialized(self, **kwargs: object) -> None:
        # Ignore this message to avoid logging a pointless warning
        pass

    def m_text_document__resolve(
        self, fileName: str, docPath: str, resolveType: str
    ) -> str:
        """Given an artifact's path relative to the project's source directory,
        return a corresponding source file path relative to the project's root."""

        if self.project is None:
            logger.warn("Project uninitialized")
            return fileName

        if resolveType == "doc":
            target = PurePath(fileName).with_suffix(".txt")
            fileid, target_path = util.reroot_path(
                target, PurePath(docPath), self.project.config.source_path
            )
            return str(target_path)
        elif resolveType == "directive":
            return str(self.project.config.source_path) + fileName
        else:
            logger.error("resolveType is not supported")
            return fileName

    def m_text_document__did_open(self, textDocument: SerializableType) -> None:
        if not self.project:
            return

        item = check_type(TextDocumentItem, textDocument)
        fileid = self.uri_to_fileid(item.uri)
        page_path = self.project.get_full_path(fileid)
        entry = WorkspaceEntry(fileid, item.uri, [])
        self.workspace[item.uri] = entry
        self.project.update(page_path, item.text)

    @debounce(0.2)
    def m_text_document__did_change(
        self, textDocument: SerializableType, contentChanges: SerializableType
    ) -> None:
        if not self.project:
            return

        identifier = check_type(VersionedTextDocumentIdentifier, textDocument)
        page_path = self.project.get_full_path(self.uri_to_fileid(identifier.uri))
        assert isinstance(contentChanges, list)
        change = next(
            check_type(TextDocumentContentChangeEvent, x) for x in contentChanges
        )
        self.project.update(page_path, change.text)

    def m_text_document__did_close(self, textDocument: SerializableType) -> None:
        if not self.project:
            return

        identifier = check_type(TextDocumentIdentifier, textDocument)
        page_path = self.project.get_full_path(self.uri_to_fileid(identifier.uri))
        del self.workspace[identifier.uri]
        self.project.update(page_path)

    def m_shutdown(self, **_kwargs: object) -> None:
        self._shutdown = True

    def m_exit(self, **_kwargs: object) -> None:
        self._endpoint.shutdown()
        if self.project:
            self.project.stop_monitoring()

    def __enter__(self) -> "LanguageServer":
        return self

    def __exit__(self, *args: object) -> None:
        self.m_shutdown()
        self.m_exit()


def start() -> None:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    server = LanguageServer(stdin, stdout)
    logger.info("Started")
    server.start()
