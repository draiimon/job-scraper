from __future__ import annotations
import base64, hashlib, hmac, json, time
from .config import Settings
class ActionTokens:
    def __init__(self,cfg:Settings): self.secret=cfg.app_secret_key.encode() if cfg.app_secret_key else None
    def issue(self,job_id:int,action:str,expires_seconds:int=86400) -> str | None:
        if not self.secret: return None
        raw=json.dumps({'j':job_id,'a':action,'e':int(time.time())+expires_seconds},separators=(',',':')).encode()
        sig=hmac.new(self.secret,raw,hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw+sig).decode().rstrip('=')
    def verify(self,token:str,action:str|None=None) -> dict | None:
        if not self.secret: return None
        try:
            data=base64.urlsafe_b64decode(token+'='*(-len(token)%4)); raw,sig=data[:-32],data[-32:]
            if not hmac.compare_digest(sig,hmac.new(self.secret,raw,hashlib.sha256).digest()): return None
            payload=json.loads(raw)
            return payload if payload['e']>=time.time() and (action is None or payload['a']==action) else None
        except (ValueError,KeyError,json.JSONDecodeError): return None
