import asyncio
import threading
import time
from collections import deque

from fastapi import Request
from pydantic import BaseModel

from loguru import logger
from app.model.api_models import GeneratorBase, GeneratorException, ApiResponse, RequestPayload


class ClientRequest:
    def __init__(self, request: Request, request_payload: RequestPayload, cnt: int):
        self.creation_time = time.time()
        self.id: str = self.get_client_id(request)
        self.cnt: int = cnt
        self.request: Request = request
        self.request_payload = request_payload
        self.api_response: ApiResponse | None = None
        self.event: asyncio.Event = asyncio.Event()

    @staticmethod
    def get_client_id(request):
        if 'authorization' in request._headers:
            auth_header = request._headers['authorization']
            logger.debug(f"auth_header {auth_header}")
            if auth_header.startswith("Bearer "):
                return auth_header[7:] + request.client.host
        return ""


class ClientRequestQueue:
    def __init__(self):
        self._queue: deque = deque()
        self._client_items: dict = dict()
        self._lock: threading.Lock = threading.Lock()
        self._cache: dict = dict()

    async def put_or_exchange(self, item: ClientRequest) -> ClientRequest | None:
        client_id: str = item.id
        exchanged_item: ClientRequest | None = None
        with self._lock:
            if client_id in self._client_items:
                exchanged_item = self._client_items[client_id]
            else:
                self._queue.append(client_id)
            self._client_items[client_id] = item
        return exchanged_item

    async def get(self) -> ClientRequest:
        while True:
            with self._lock:
                if self._queue:
                    client_id = self._queue.popleft()
                    item = self._client_items.pop(client_id)
                    return item
            await asyncio.sleep(.01)


class ResponseCache:
    def __init__(self):
        self._cache: dict[tuple, ApiResponse] = dict()
        self._lock: threading.Lock = threading.Lock()

    async def update(self, request_payload: RequestPayload, api_response: ApiResponse):
        with self._lock:
            self._cache[request_payload.key()] = api_response

    async def retrieve(self, request_payload: RequestPayload) -> ApiResponse | None:
        with self._lock:
            if request_payload.key() in self._cache:
                api_response = self._cache[request_payload.key()]
                api_response.set_is_cached_response()
                return api_response
        return None


class RequestHandler:
    def __init__(self, generator: GeneratorBase):
        self.generator: GeneratorBase = generator
        self.queue: ClientRequestQueue = ClientRequestQueue()
        self.response_cache: ResponseCache = ResponseCache()
        self.cnt = 0

    async def process_request_queue(self):
        while True:
            logger.debug("awaiting next request")
            client_request: ClientRequest = await self.queue.get()
            request: Request = client_request.request
            request_payload = client_request.request_payload
            logger.debug(f"got request {client_request.cnt} from queue {request.client.port}")
            api_response: ApiResponse = await self.response_cache.retrieve(request_payload)
            try:
                if api_response is None:
                    await asyncio.sleep(0.005)
                    api_response = await self.generator.generate(request_payload)
                    await self.response_cache.update(request_payload, api_response)
                else:
                    logger.debug(f"cache hit for request {client_request.cnt}")
            except GeneratorException as e:
                # pass the error message as generated text, so that the user will see it within the IDE
                api_response = self.generator.generate_default_api_response(str(e), 400)
            logger.debug(f"done processing request {client_request.cnt} from queue {request.client.port}")
            client_request.api_response = api_response
            client_request.event.set()

    async def handle_request(self, request: Request, request_payload: RequestPayload) -> BaseModel:
        self.cnt += 1
        local_cnt = self.cnt
        logger.info(f" received request {local_cnt} from {request.client.host}:{request.client.port}")
        client_request: ClientRequest = ClientRequest(request, request_payload, local_cnt)

        cached_response = await self.response_cache.retrieve(request_payload)
        if cached_response is not None:
            logger.debug(f"cache hit for request {local_cnt}")
            logger.info(
                f" returning request {local_cnt} from port {request.client.port}: time {time.time() - client_request.creation_time:.5f}")
            return cached_response

        exchanged_client_request = await self.queue.put_or_exchange(client_request)
        if exchanged_client_request is not None:
            logger.info(f" expired request {exchanged_client_request.cnt}")
            exchanged_client_request.api_response = self.generator.generate_default_api_response("", 429)
            exchanged_client_request.event.set()

        logger.debug(f"waiting for request {local_cnt}")
        await client_request.event.wait()

        logger.info(
            f" returning request {local_cnt} from port {request.client.port}: time {time.time() - client_request.creation_time:.5f}")
        return client_request.api_response


class RequestHandlerProvider:
    def __init__(self, request_handler: RequestHandler):
        self.request_handler = request_handler

    def get_handler(self) -> RequestHandler:
        return self.request_handler
