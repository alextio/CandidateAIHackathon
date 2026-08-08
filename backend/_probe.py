import sys, time
def log(m): print(f"[{time.time():.1f}] {m}", flush=True)
t0=time.time()
from app.config import get_settings
from app.db import get_client
from app.map_view import _load_ercot, _load_links, _load_map_base
log("imports done")
s=get_settings(); c=get_client(s)
log("client ready")
t=time.time(); e=_load_ercot(c); log(f"_load_ercot: {len(e)} rows in {time.time()-t:.1f}s")
t=time.time(); l=_load_links(c); log(f"_load_links: {len(l)} rows in {time.time()-t:.1f}s")
t=time.time(); base=_load_map_base(c); log(f"_load_map_base done in {time.time()-t:.1f}s")
log(f"TOTAL {time.time()-t0:.1f}s")
