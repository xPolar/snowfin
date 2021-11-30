import asyncio
from typing import Coroutine

from sanic import Sanic, Request
from sanic.response import HTTPResponse, json
from sanic.log import logger

from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from dacite import from_dict, config

import snowfin.interaction
from .commands import InteractionHandler
from .response import _DiscordResponse, DeferredResponse
from .http import *
from .enums import *

__all__ = (
    'SlashCommand',
    'MessageComponent',
    'Autocomplete',
    'Modal',
    'Client'
)

def mix_into_commands(func: Coroutine, type: RequestType, name: str = None, **kwargs) -> Coroutine:
    """
    A global wrapper of a wrapper to add routines to a class object
    """
    async def wrapper(*args, **kwargs):
        result = await func(*args, **kwargs)
        return result

    InteractionHandler.register(wrapper, type, name, **kwargs)
    return wrapper

def SlashCommand(name: str = None, type: CommandType = None) -> Coroutine:
    """
    A decorator for creating slash command callbacks. If name and type are supplied, name is used.
    """
    def decorator(func):
        return mix_into_commands(func, RequestType.APPLICATION_COMMAND, name, command_type=type)
    return decorator

def MessageComponent(custom_id: str = None, type: ComponentType = None) -> Coroutine:
    """
    A decorator for creating message component callbacks. If custom_id and type are supplied, custom_id is used.
    """
    def decorator(func):
        return mix_into_commands(func, RequestType.MESSAGE_COMPONENT, custom_id, component_type=type)
    return decorator

def Autocomplete(name: str = None) -> Coroutine:
    """
    A decorator for creating autocomplete callbacks.
    """
    def decorator(func):
        return mix_into_commands(func, RequestType.APPLICATION_COMMAND_AUTOCOMPLETE, name)
    return decorator

def Modal(custom_id: str = None) -> Coroutine:
    """
    A decorator for creating modal submit callbacks.
    """
    def decorator(func):
        return mix_into_commands(func, RequestType.MODAL_SUBMIT, custom_id)
    return decorator


class Client:

    def __init__(self, verify_key: str, app: Sanic = None, defer_automatically: bool = False, auto_defer_timeout: float = 2.8, auto_defer_ephemeral: bool = False, **kwargs):
        # create a new app if none is not supplied
        if app is None:
            self.app = Sanic("snowfin-interactions")
        else:
            self.app = app
        self.verify_key = VerifyKey(bytes.fromhex(verify_key))

        # automatic defer options
        self.defer_automatically = defer_automatically
        self.auto_defer_timeout = auto_defer_timeout

        # branchless programming baby! 
        self.auto_defer_flags = 64 * int(auto_defer_ephemeral)

        self.http: HTTP = HTTP(**dict((x, kwargs.get(x, None)) for x in ('proxy', 'proxy_auth', 'headers')))

        # create middlware for verifying that discord is the one who sent the interaction
        @self.app.on_request
        async def verify_signature(request: Request):
            signature = request.headers["X-Signature-Ed25519"]
            timestamp = request.headers["X-Signature-Timestamp"]
            body = request.body.decode("utf-8")

            try:
                self.verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
            except BadSignatureError:
                return json({"error": "invalid signature"}, status=403)

        # middlware for constructing dataclasses from json
        @self.app.on_request
        async def parse_request(request: Request):
            request.ctx = from_dict(
                data= request.json,
                data_class=snowfin.interaction.Interaction,
                config=config.Config(
                    cast=[
                        int,
                        ChannelType,
                        CommandType,
                        OptionType,
                        ComponentType,
                        RequestType
                    ]
                )
            )

        # send PONGs to PINGs
        @self.app.on_request
        async def ack_request(request: Request):
            if request.ctx.type == RequestType.PING:
                return json({"type": 1})

        # handle user callbacks
        @self.app.post("/")
        async def handle_request(request: Request):
            return await self.handle_request(request)


    def handle_deferred_routine(self, routine: asyncio.Task, request):
        """
        Create a wrapper for the task supplied and wait on it.
        log any errors and pass the result onward
        """
        async def wrapper():
            try:
                response = await routine
                await self.handle_deferred_response(request, response)
            except Exception as e:
                logger.error(e.__repr__())
        task = asyncio.get_event_loop().create_task(wrapper())

    async def handle_deferred_response(self, request, response):
        """
        Take the result of a deferred callback task and send a request to the interaction webhook
        """
        if response:
            if response.type in (ResponseType.SEND_MESSAGE, ResponseType.EDIT_ORIGINAL_MESSAGE):
                await self.http.edit_original_message(request, response)
            else:
                raise Exception("Invalid response type")

    async def handle_request(self, request: Request) -> HTTPResponse:
        """
        Grab the callback Coroutine and create a task.
        """
        func = InteractionHandler.get_func(request.ctx.data, request.ctx.type)
        if func:
            task = asyncio.create_task(func(request))

            if self.defer_automatically:
                # we want to defer automatically and keep the original task going
                # so we wait for up to the timeout, then construct a DeferredResponse ourselves
                # then handle_deferred_routine() will do the rest
                done, pending = await asyncio.wait([task], timeout = self.auto_defer_timeout)

                if task in pending:
                    # task didn't return in time, let it keep going and defer for it
                    resp = DeferredResponse(task)
                else:
                    # the task returned in time, get the result and use that like normal
                    resp = task.result()
            else:
                resp = await task
        else:
            return json({"error": "command not found"}, status=404)

        request.ctx.responded = True

        if isinstance(resp, _DiscordResponse):
            if isinstance(resp, DeferredResponse):
                # make sure we are sending the correct interaction response type for the request
                if request.ctx.type == RequestType.MESSAGE_COMPONENT:
                    resp.type = ResponseType.COMPONENT_DEFER
                else:
                    resp.type = ResponseType.DEFER

                # if someone passed in a callable, construct a task for them to keep syntax as clean as possible
                if not isinstance(resp.task, asyncio.Task):
                    resp.task = asyncio.create_task(resp.task(request))

                # start or continue the task and post the response to a webhook
                self.handle_deferred_routine(resp.task, request)
            return json(resp.to_dict())

        elif isinstance(resp, HTTPResponse):
            # someone gave us a sanic response, Assume they know what they are doing
            return resp
            
        else:
            return json({"error": "invalid response type"}, status=500)

    def run(self, host: str, port: int, **kwargs):
        self.app.run(host=host, port=port, **kwargs)