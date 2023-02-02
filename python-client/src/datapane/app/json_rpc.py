from __future__ import annotations

import typing as t

import bottle as bt
from pydantic import BaseModel, ValidationError, conint, constr, validator

from datapane.client import log

from .runtime import GlobalState, apply_ref, get_session_state, global_rpc_functions

RPC_JSON = t.Union[str, int, float, bool, None, t.Mapping[str, t.Any], t.List[t.Any]]
JListDict = t.Union[t.List[t.Any], t.Dict[str, t.Any]]


class RpcRequest(BaseModel):
    jsonrpc: str
    # if id not present is considered to be notification, i.e. async
    id: t.Optional[conint(strict=True, gt=0)] = None
    method: constr(strip_whitespace=True, regex=r"^[\w.]+$", min_length=3)  # noqa: F722
    # missing params same as empty
    params: t.Optional[JListDict] = []

    @validator("jsonrpc")
    def is_v2(cls, v: str) -> str:
        assert v == "2.0"
        return v

    @property
    def is_method_call(self) -> bool:
        return self.id is not None

    class Config:
        allow_mutation = False


class RpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: int
    result: RPC_JSON


class RpcErrorMessage(BaseModel):
    code: int
    message: str
    data: t.Optional[RPC_JSON] = None


class RpcError(BaseModel):
    json_rpc: str = "2.0"
    id: t.Optional[int] = None
    error: RpcErrorMessage


# TODO - RPC exceptions -> error codes?
class RPCException(Exception):
    def __init__(self, code: int, msg: str, _id: t.Optional[int] = None, data: t.Optional[RPC_JSON] = None):
        self.code = code
        self.msg = msg
        self.id = _id
        self.data = data

    def as_rpc(self) -> RpcError:
        return RpcError(id=self.id, error=RpcErrorMessage(code=self.code, message=self.msg, data=self.data))


##################################################################
# RPC Dispatcher
def dispatch(g_s: GlobalState) -> t.Dict:
    try:
        return _dispatch(g_s)
    except RPCException as e:
        # convert to a JSON-RPC error msg
        log.exception(e)
        return e.as_rpc().dict()


def _dispatch(g_s: GlobalState) -> t.Optional[t.Dict]:
    """JSON-RPC dispatcher"""

    s_s = get_session_state()
    # NOTE - we don't handle RPC Parse error here, bottle will return 400 instead if invalid json
    json_req_obj: t.Dict = bt.request.json
    _id: t.Optional[int] = None

    # parse the request object
    try:
        # NOTE - do we trust the client, can we use .construct() and bypass validation?
        json_req: RpcRequest = RpcRequest.parse_obj(json_req_obj)
        _id = json_req.id
    except ValidationError as e:
        log.exception(e)
        raise RPCException(-32600, "Invalid Request", _id)

    # process args
    if isinstance(json_req.params, list):
        args = json_req.params.copy()
        kwargs = dict()
    else:  # isinstance(params, dict):
        kwargs = json_req.params.copy()
        args = tuple()

    # dispatch method call
    if json_req.is_method_call:
        try:
            log.info(f"Attempting to dispatch to {json_req.method}")
            f_name = json_req.method

            if f_name.startswith("dp.") and f_name in global_rpc_functions:
                # global functions are called directly
                f = global_rpc_functions[json_req.method]
                res = f(g_s, s_s, *args, **kwargs)
            elif f_name.startswith("app.") and f_name in s_s.entries:
                # app function
                # move into helper?
                ref = s_s.entries[json_req.method]
                res = apply_ref(g_s, s_s, ref, params=kwargs)
            else:
                raise RPCException(-32601, f"Method '{f_name}' not found", _id)

            return RpcResponse(id=json_req.id, result=res).dict()
        except RPCException as e:
            raise e
        except Exception as e:
            raise RPCException(-32603, "Internal Error", _id) from e
    else:
        log.warning(f"Got a (unsupported) notification, ignoring - {json_req}")
        return None