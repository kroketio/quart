import asyncio
import io
from cgi import FieldStorage, parse_header
from typing import Any, AnyStr, Callable, Generator, Optional, TYPE_CHECKING
from urllib.parse import parse_qs

from ._base import BaseRequestWebsocket, JSONMixin
from ..datastructures import CIMultiDict, FileStorage, MultiDict

if TYPE_CHECKING:
    from .routing import Rule  # noqa


class Body:
    """A request body container.

    The request body can either be iterated over and consumed in parts
    (without building up memory usage) or awaited.

    .. code-block:: python

        async for data in body:
            ...
        # or simply
        complete = await body

    Note: It is not possible to iterate over the data and then await
    it.
    """

    def __init__(self, max_content_length: Optional[int]) -> None:
        self._body: asyncio.Future = asyncio.Future()
        self._stream: asyncio.Queue = asyncio.Queue()
        self._size = 0
        self._max_content_length = max_content_length

    def __aiter__(self) -> 'Body':
        return self

    async def __anext__(self) -> bytes:
        # The iterator should return whenever there is any data, but
        # quit if the body future is done i.e. there is no more data.
        done, _ = await asyncio.wait(  # type: ignore
            [self._body, self._stream.get()], return_when=asyncio.FIRST_COMPLETED,  # type: ignore
        )
        if self._body.done():
            raise StopAsyncIteration()
        else:
            result = bytearray()
            for future in done:
                data = future.result()
                result.extend(data)
                self._size -= len(data)
            return result

    def __await__(self) -> Generator[Any, None, Any]:
        return self._body.__await__()

    def append(self, data: bytes) -> None:
        self._stream.put_nowait(data)
        self._size += len(data)
        if self._max_content_length is not None and self._size > self._max_content_length:
            from ..exceptions import RequestEntityTooLarge  # noqa Avoiding circular import
            raise RequestEntityTooLarge()

    def set_complete(self) -> None:
        buffer_ = bytearray()
        try:
            while True:
                buffer_.extend(self._stream.get_nowait())
        except asyncio.QueueEmpty:
            self._body.set_result(buffer_)

    def set_result(self, data: bytes) -> None:
        """Convienience method, mainly for testing."""
        self._body.set_result(data)


class Request(BaseRequestWebsocket, JSONMixin):
    """This class represents a request.

    It can be subclassed and the subclassed used in preference by
    replacing the :attr:`~quart.Quart.request_class` with your
    subclass.
    """

    def __init__(
            self,
            method: str,
            path: str,
            headers: CIMultiDict,
            *,
            max_content_length: Optional[int]=None,
    ) -> None:
        """Create a request object.

        Arguments:
            method: The HTTP verb.
            path: The full URL of the request.
            headers: The request headers.
            body: An awaitable future for the body data i.e.
                ``data = await body``
            max_content_length: The maximum length in bytes of the
                body (None implies no limit in Quart).
        """
        super().__init__(method, path, headers)
        content_length = headers.get('Content-Length')
        self.max_content_length = max_content_length
        if (
                content_length is not None and self.max_content_length is not None and
                int(content_length) > self.max_content_length
        ):
            from ..exceptions import RequestEntityTooLarge  # noqa Avoiding circular import
            raise RequestEntityTooLarge()
        self.body = Body(self.max_content_length)
        self._form: Optional[MultiDict] = None
        self._files: Optional[MultiDict] = None

    async def get_data(self, raw: bool=True) -> AnyStr:
        """The request body data."""
        if raw:
            return await self.body  # type: ignore
        else:
            return (await self.body).decode(self.charset)  # type: ignore

    @property
    async def form(self) -> MultiDict:
        """The parsed form encoded data.

        Note file data is present in the :attr:`files`.
        """
        if self._form is None:
            await self._load_form_data()
        return self._form

    @property
    async def files(self) -> MultiDict:
        """The parsed files.

        This will return an empty multidict unless the request
        mimetype was ``enctype="multipart/form-data"`` and the method
        POST, PUT, or PATCH.
        """
        if self._files is None:
            await self._load_form_data()
        return self._files

    async def _load_form_data(self) -> None:
        data = await self.body  # type: ignore
        self._form = MultiDict()
        self._files = MultiDict()
        content_header = self.headers.get('Content-Type')
        if content_header is None:
            return
        content_type, parameters = parse_header(content_header)
        if content_type == 'application/x-www-form-urlencoded':
            for key, values in parse_qs(data.decode()).items():
                for value in values:
                    self._form[key] = value
        elif content_type == 'multipart/form-data':
            field_storage = FieldStorage(
                io.BytesIO(data), headers=self.headers, environ={'REQUEST_METHOD': 'POST'},
            )
            for key in field_storage:  # type: ignore
                field_storage_key = field_storage[key]
                if field_storage_key.filename is None:
                    self._form[key] = field_storage_key.value
                else:
                    self._files[key] = FileStorage(
                        io.BytesIO(field_storage_key.file.read()), field_storage_key.filename,
                        field_storage_key.name, field_storage_key.type, field_storage_key.headers,
                    )

    async def _load_json_data(self) -> str:
        """Return the data after decoding."""
        return await self.get_data(raw=False)


class Websocket(BaseRequestWebsocket):

    def __init__(
            self,
            path: str,
            headers: CIMultiDict,
            queue: asyncio.Queue,
            send: Callable,
    ) -> None:
        """Create a request object.

        Arguments:
            method: The HTTP verb.
            path: The full URL of the request.
            headers: The request headers.
            websocket: The actual websocket with the data.
        """
        super().__init__('GET', path, headers)
        self._queue = queue
        self._send = send

    async def receive(self) -> bytes:
        return await self._queue.get()

    async def send(self, data: bytes) -> None:
        self._send(data)
