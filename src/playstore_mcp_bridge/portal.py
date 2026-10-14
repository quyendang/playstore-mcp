"""A small same-origin file portal; browser login uses verified Google OIDC."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx2
import jwt
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .identity import allowed_identity, decode_signed

if TYPE_CHECKING:
    from starlette.requests import Request

    from .config import Settings

COOKIE = "bridge_session"
STATE_COOKIE = "bridge_login"


def signed(claims: dict, settings: Settings, audience: str, ttl: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {**claims, "iss": settings.public_url, "aud": audience, "iat": now, "exp": now + ttl},
        settings.signing_key,
        algorithm="HS256",
    )


def login(settings: Settings) -> Response:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": settings.oauth_client_id,
            "redirect_uri": f"{settings.public_url}/portal/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
    )
    response = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")
    response.set_cookie(
        STATE_COOKIE,
        signed({"state": state, "nonce": nonce}, settings, "portal-login", 300),
        max_age=300,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/portal",
    )
    return response


async def callback(request: Request, settings: Settings) -> Response:
    response: Response
    try:
        state = decode_signed(request.cookies.get(STATE_COOKIE, ""), settings, "portal-login")
        if not secrets.compare_digest(str(state["state"]), request.query_params.get("state", "")):
            raise ValueError("Invalid login state")
        code = request.query_params.get("code")
        if not code:
            raise ValueError("Google authorization was not completed")
        async with httpx2.AsyncClient(timeout=20) as client:
            result = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.oauth_client_id,
                    "client_secret": settings.oauth_client_secret,
                    "redirect_uri": f"{settings.public_url}/portal/callback",
                    "grant_type": "authorization_code",
                },
            )
        result.raise_for_status()
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            result.json()["id_token"],
            GoogleRequest(),
            settings.oauth_client_id,
        )
        if not secrets.compare_digest(str(claims.get("nonce", "")), str(state["nonce"])):
            raise ValueError("Invalid login nonce")
        owner = allowed_identity(claims, settings)
        response = RedirectResponse("/portal", status_code=303)
        response.set_cookie(
            COOKIE,
            signed(
                {
                    "sub": owner,
                    "email": claims.get("email"),
                    "email_verified": claims.get("email_verified"),
                },
                settings,
                "portal",
                3600,
            ),
            max_age=3600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
    except Exception:
        response = JSONResponse(
            {"error": "Login failed or account is not allowed"}, status_code=403
        )
    response.delete_cookie(STATE_COOKIE, path="/portal", secure=True, httponly=True, samesite="lax")
    return response


def page() -> HTMLResponse:
    html = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Play Store files</title>
<link rel="stylesheet" href="/portal/style.css"><main><h1>Play Store files</h1><p>Upload an artifact, then use its asset:// reference in ChatGPT or Gemini. Files are shared with the allowed team.</p>
<form id="upload"><label>APK, AAB, screenshot, expansion or deobfuscation file<input type="file" id="file" required></label><label>Resume an existing upload (optional)<input id="resume" placeholder="Asset ID"></label><button>Upload</button></form>
<progress id="progress" value="0" max="100"></progress><p id="status" role="status"></p><button id="refresh">Refresh files</button><div id="assets"></div><p><a href="/mcp">MCP endpoint</a></p><form action="/portal/logout" method="post"><button>Sign out</button></form></main><script src="/portal/script.js" defer></script></html>"""
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


STYLE = """body{font:17px/1.6 system-ui,sans-serif;background:#f7f7f5;color:#222;margin:0}main{max-width:850px;padding:40px 24px;margin:auto}h1{font-size:32px}label{display:block;margin:18px 0}input{display:block;margin-top:6px;font:inherit;max-width:100%;box-sizing:border-box}button{font:inherit;padding:8px 16px;cursor:pointer}progress{width:100%;margin-top:20px}.asset{padding:16px 0;border-bottom:1px solid #ccc}code{overflow-wrap:anywhere}a{color:#164caf}.asset button{margin-left:12px}#status{white-space:pre-wrap}"""


def script(chunk_size: int) -> str:
    return r"""
const $=id=>document.getElementById(id);
async function api(path, options={}) {
 const response=await fetch(path, options);
 if(response.status===401){location.href='/portal/login';throw Error('Sign in again');}
 const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');return data;
}
function node(tag,text){const e=document.createElement(tag);e.textContent=text;return e;}
async function refresh(){
 const assets=await api('/api/assets');$('assets').replaceChildren();
 for(const a of assets){
  const row=node('div','');row.className='asset';row.append(node('strong',a.name+' · '+a.status+' · '+a.size+' bytes'),node('p',a.reference));
  const copy=node('button','Copy reference');copy.onclick=()=>navigator.clipboard.writeText(a.reference);row.append(copy);
  if(a.status==='ready'){const link=node('a','Download');link.href='/files/'+a.id;row.append(link);}
  const remove=node('button','Delete');remove.onclick=async()=>{if(confirm('Delete '+a.name+'?')){await api('/api/assets/'+a.id,{method:'DELETE'});await refresh();}};row.append(remove);$('assets').append(row);
 }
}
$('upload').onsubmit=async e=>{
 e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
 try{
  const f=$('file').files[0];if(!f)throw Error('Choose a file');
  const resume=$('resume').value.trim();let a=resume?await api('/api/assets/'+encodeURIComponent(resume)):await api('/api/assets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:f.name,size:f.size})});
  if(a.size!==f.size||a.name!==f.name)throw Error('Resumed file name/size must match');
  $('resume').value=a.id;$('status').textContent='Upload ID: '+a.id;
  while(a.offset<f.size){a=await api('/api/assets/'+a.id+'/chunks?offset='+a.offset,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:f.slice(a.offset,Math.min(f.size,a.offset+CHUNK))});$('progress').value=f.size?100*a.offset/f.size:100;}
  a=await api('/api/assets/'+a.id+'/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  $('progress').value=100;$('status').textContent='Ready: '+a.reference+'\nSHA-256: '+a.sha256;$('resume').value='';await refresh();
 }catch(err){$('status').textContent=err.message+'\nKeep the upload ID to resume.';}finally{button.disabled=false;}
};
$('refresh').onclick=()=>refresh().catch(e=>$('status').textContent=e.message);
refresh().catch(e=>$('status').textContent=e.message);
""".replace("CHUNK", str(min(chunk_size, 5 * 1024**2)))
