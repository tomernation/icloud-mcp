#!/usr/bin/python3
from pathlib import Path
import urllib.request,urllib.parse,sys
try:
 token=Path('/etc/duckdns/token').read_text().strip()
 if not token or any(c in token for c in '\r\n\0'):raise ValueError()
 body=urllib.parse.urlencode({'domains':'chasid','token':token,'ip':''}).encode()
 req=urllib.request.Request('https://www.duckdns.org/update?'+body.decode())
 with urllib.request.urlopen(req,timeout=20) as response:result=response.read(128).decode().strip()
 if result!='OK':raise ValueError()
 print('DuckDNS update successful')
except Exception:
 print('DuckDNS update failed; details suppressed to protect token',file=sys.stderr);sys.exit(1)
